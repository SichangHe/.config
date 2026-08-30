#!/usr/bin/env python3
"""Create/link a markdown task and optionally start a worker tmux window."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

try:
    from omo_manager.omo_codex_status import current_block, exact_pane_id, status, tail
    from omo_manager.omo_agent_status import DEFAULT_ROOT, TaskFrontmatterError, parse_task_metadata
    from omo_manager.omo_blocking import V2_VERSION, generated_id, load_yaml_mapping, render_task, split_task_text, v2_enabled
    from omo_manager.omo_manager_rotate import RotationError, is_codex_launch_argv, process_is_under, read_processes
    from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TASK_FRONTMATTER_V2, first_version, frontmatter_text
    from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
except ModuleNotFoundError:
    from omo_codex_status import current_block, exact_pane_id, status, tail
    from omo_agent_status import DEFAULT_ROOT, TaskFrontmatterError, parse_task_metadata
    from omo_blocking import V2_VERSION, generated_id, load_yaml_mapping, render_task, split_task_text, v2_enabled
    from omo_manager_rotate import RotationError, is_codex_launch_argv, process_is_under, read_processes
    from omo_task_metadata import TASK_FRONTMATTER_V1, TASK_FRONTMATTER_V2, first_version, frontmatter_text
    from omo_task_lock import process_start_ticks, task_file_lock, task_target_lock

HELPER_DIR = Path(__file__).resolve().parent
DEFAULT_WORKER_INSTRUCTIONS = HELPER_DIR / "WORKER_DEFAULTS.md"
VL_WORKER_INSTRUCTIONS = HELPER_DIR / "VL_WORKER_DEFAULTS.md"
PCODX_WRAPPER = HELPER_DIR / "pcodx"
COMMAND_BY_TOOL = {
    "codex": ("bunx", "@openai/codex@latest", "--dangerously-bypass-approvals-and-sandbox"),
    "pcodx": (str(PCODX_WRAPPER),),
    "cursor": ("agent", "--force", "--sandbox", "disabled", "--trust"),
}
DEFAULT_TOOL = "cursor"
TASK_FRONTMATTER_VERSION = "v1.0.0"
TMUX_TARGET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?$")
TMUX_SESSION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
TMUX_SESSION_ID_RE = re.compile(r"^\$\d+$")
SHELL_COMMANDS = {"bash", "dash", "fish", "sh", "zsh"}
CODEX_LAUNCH_PANE_COMMANDS = {"bunx"}
BULLET_MARKERS = ("- ", "* ")
PENDING_TASK_ITEMS_MARKER = "(above are pending task items)"
TASK_METADATA_PREFIXES = ("managerat:",)
CODEX_LAUNCH_STARTED = "started"
CODEX_LAUNCH_UPDATED = "updated"
CODEX_LAUNCH_MARKER_PREFIX = "[omo:"
CODEX_LAUNCH_MARKER_DRY_RUN = f"{CODEX_LAUNCH_MARKER_PREFIX}DRY]"
CODEX_UPDATE_PROMPT_MARKERS = ("update available!", "update now", "press enter to continue")
CODEX_UPDATE_SUCCESS_MARKERS = ("update ran successfully", "please restart codex")
CODEX_TRUST_TEXT = "Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection. Trusting the directory allows project-local config, hooks, and exec policies to load."
CODEX_TRUST_CWD_RE = re.compile(r"^> You are in \S.*$")
CODEX_TRUST_NOTE_RE = re.compile(r"^Note: You’re in a subdirectory of a Git project\. Trusting will apply to the repository root: \S.*$")
CODEX_TRUST_YES_RE = re.compile(r"^\s*› 1\. Yes, continue\s*$")
CODEX_TRUST_NO_RE = re.compile(r"^\s*2\. No, quit\s*$")
CODEX_TRUST_CONFIRM_RE = re.compile(r"^\s*Press enter to continue(?: and create a sandbox\.\.\.)?\s*$")


def root_membership_lock(root: Path):
    """Serialize task creation and manager-owner membership changes."""
    return task_file_lock(root / ".omo-task-membership.lock")


def infer_work_log_root(path: Path) -> Path:
    """Find the nearest ancestor whose TODO index identifies the work-log root."""
    resolved = path.resolve(strict=False)
    for candidate in (resolved.parent, *resolved.parents):
        if (candidate / "TODO.md").is_file():
            return candidate
    raise ValueError("manager-owner migration requires --root when no ancestor TODO.md identifies the work-log root.")


MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
LINE_RANGE_RE = re.compile(r"^([1-9]\d*)-([1-9]\d*)$")
HUMAN_INSTRUCTION_CLOSE = "</human_instruction>"
HUMAN_INSTRUCTION_OPEN = "<human_instruction"
MANAGER_DELEGATION_CLOSE = "</manager_delegation>"
HUMAN_LAUNCH_REQUEST_RE = re.compile(
    r"^\s*(?:(?:no|yes|well)\s*,\s*)?(?:(?:please|just)\s+)?(?:"
    r"(?:launch|create|start|open|spawn)\b|"
    r"(?:give\s+me|set\s+up|i\s+(?:want|need|would\s+like)|i(?:\s+am|['’]m)\s+asking\s+you\s+to)\b.*\b(?:agent|manager|worker)\b"
    r")",
    flags=re.IGNORECASE,
)
HUMAN_DIRECT_LAUNCH_TARGET_RE = re.compile(
    r"^\s*(?:please\s+)?(?:launch|create|start|open|spawn)\s+[`'\"]?([A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)",
    flags=re.IGNORECASE,
)
HUMAN_LAUNCH_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|without|cannot|cant|dont|wont|wouldnt|shouldnt|couldnt|mustnt|isnt|arent|wasnt|werent|doesnt|didnt|havent|hasnt|hadnt|neednt|shant|[a-z]+n['‘’ʼ＇]t)\b",
    flags=re.IGNORECASE,
)
HUMAN_LAUNCH_DISCOURSE_NEGATION_RE = re.compile(r"^\s*no\s*,\s*", flags=re.IGNORECASE)
HUMAN_LAUNCH_ROLE_RE = re.compile(r"\b(?:agent|manager|worker|window|pane|session)\b", flags=re.IGNORECASE)
HUMAN_LAUNCH_ROLE_TARGET_RE = re.compile(
    r"\b(?:in|into|at)\s+[`'\"]?([A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?)",
    flags=re.IGNORECASE,
)
DEFAULT_LONG_RUNNING_BLOCKED_ON = "persistent manager role"
AMH_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    tmux_session: str
    tmux_window: str
    tool: str
    workdir: Path | None
    window_name: str
    prompt_file: Path | None
    no_link: bool
    dry_run: bool
    session_id: str
    reasoning_effort: str
    codex_flags: tuple[str, ...]
    tool_explicit: bool = False
    manager_target: str = ""
    prelaunch_source: Path | None = None
    is_manager: bool = False
    migrate_manager_owner: bool = False
    old_manager_target: str = ""
    new_manager_target: str = ""
    model: str = ""
    human_email_file: Path | None = None
    human_email_lines: tuple[int, int] | None = None
    human_email_text: str | None = None
    resume_idle: bool = False
    amh_caller_agent: str = ""
    require_existing_tmux_session: bool = False
    allow_new_tmux_session: bool = False
    prepared_successor_journal: Path | None = None
    expected_prepared_journal_sha256: str = ""
    expected_prepared_task_sha256: str = ""
    expected_prepared_prompt_sha256: str = ""
    expected_prepared_queue_sha256: str = ""
    expected_prepared_launch_manifest_sha256: str = ""
    prepared_runtime_path: Path | None = None
    prepared_shell_path: Path | None = None
    prepared_env_path: Path | None = None
    prepared_launch_environment: tuple[tuple[str, str], ...] = ()
    prepared_process_environment: tuple[tuple[str, str], ...] = ()
    prepared_tmux_path: Path | None = None
    prepared_tmux_environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class LaunchWindow:
    target: str
    pane_id: str
    session_id: str
    created_session: bool = False
    session_name: str = ""
    window_id: str = ""


@dataclass(frozen=True)
class CursorProcessProof:
    pid: int
    executable: Path
    argv: tuple[str, ...]
    argv_sha256: str


@dataclass(frozen=True)
class TmuxPaneCreationIdentity:
    session_id: str
    window_id: str
    pane_id: str
    target: str
    pane_pid: int
    process_start_ticks: int


@dataclass(frozen=True)
class PreparedLaunchReceipt:
    state: str
    target: str
    pane_id: str
    pane_pid: int
    session_id: str
    window_id: str
    process_pid: int
    process_argv_sha256: str
    protected_inventory_sha256: str


class LaunchTarget(str):
    """String-compatible target carrying the identity returned by new-window."""

    pane_id: str
    session_id: str
    created_session: bool
    session_name: str

    def __new__(cls, target: str, pane_id: str, session_id: str, created_session: bool = False, session_name: str = "") -> LaunchTarget:
        value = super().__new__(cls, target)
        value.pane_id = pane_id
        value.session_id = session_id
        value.created_session = created_session
        value.session_name = session_name
        return value


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    task_file: str = ""
    tmux_session: str = ""
    tmux_window: str = ""
    tool: str = DEFAULT_TOOL
    workdir: Path | None = None
    window_name: str = ""
    prompt_file: Path | None = None
    no_link: bool = False
    dry_run: bool = False
    session_id: str = ""
    model: str = ""
    reasoning_effort: str = ""
    codex_flag: list[str] | None = None
    manager_target: str = ""
    prelaunch_source: Path | None = None
    is_manager: bool = False
    migrate_manager_owner: bool = False
    old_manager_target: str = ""
    new_manager_target: str = ""
    human_email_file: Path | None = None
    human_email_lines: tuple[int, int] | None = None
    resume_idle: bool = False
    amh_caller_agent: str = ""
    require_existing_tmux_session: bool = False
    allow_new_tmux_session: bool = False
    prepared_successor_journal: Path | None = None
    expected_prepared_journal_sha256: str = ""
    expected_prepared_task_sha256: str = ""
    expected_prepared_prompt_sha256: str = ""
    expected_prepared_queue_sha256: str = ""
    expected_prepared_launch_manifest_sha256: str = ""


@dataclass(frozen=True)
class LaunchSession:
    name: str
    target: str
    create: bool = False


def codex_flags_model_error(codex_flags: tuple[str, ...]) -> str:
    if any(flag == "--model" or flag.startswith("--model=") or flag.startswith("-m") for flag in codex_flags):
        return "raw model selection in --codex-flag is not supported; use --model MODEL."
    return ""


def model_error(model: str) -> str:
    if model and MODEL_RE.fullmatch(model) is None:
        return "--model must be a nonempty model identifier containing only letters, numbers, `.`, `_`, `:`, `/`, or `-`."
    if model == "gpt-5.6":
        return "--model gpt-5.6 is not a supported Codex model id; use gpt-5.6-sol, gpt-5.6-terra, or gpt-5.6-luna."
    return ""


def line_range(value: str) -> tuple[int, int]:
    match = LINE_RANGE_RE.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("must be START-END with positive, inclusive line numbers")
    start, end = (int(part) for part in match.groups())
    if start > end:
        raise argparse.ArgumentTypeError("START must be less than or equal to END")
    return start, end


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Launch behavior:
  With --workdir, create or update task frontmatter, link the task in TODO.md
  unless --no-link is passed, open a tmux window with its normal shell, and
  start Cursor Agent there unless --tool codex or --tool pcodx is requested.
  This does not stop already running Codex panes. --prompt-file becomes the
  worker's initial prompt argument. Every new launch requires --model and
  --reasoning-effort; model selection in --codex-flag is rejected. Pass
  --is-manager for manager launches. WORKER_DEFAULTS.md is injected into every
  prompt, followed by MANAGER.md for manager launches. Do not repeat
  instructions to read those files. For a launch caused by email, pass
  --human-email-file and the exact relevant --human-email-lines. Keep
  --prompt-file narrowly task-specific. Keep --task-file as manager-side
  bookkeeping and out of worker prompts.

Model guidance:
  For Codex, gpt-5.6-sol medium is the default; use max for hard tasks and ultra only for
  very hard tasks. Use gpt-5.6-sol low for submanagers, gpt-5.6-terra medium for
  easier routine tasks, and gpt-5.6-luna xhigh for trivial minimal tasks. Terra
  and Luna are unreliable decision makers.
  For Cursor Agent, use model cursor-grok-4.6 with reasoning effort xhigh; the
  launcher passes that to Cursor as cursor-grok-4.6-xhigh.

Ownership migration:
  omo_task.py --root ROOT --task-file TASK.md --migrate-manager-owner --old-manager-target OLD --new-manager-target NEW [--dry-run]""",
    )
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--task-file", required=True)
    _ = parser.add_argument("--tmux-session", default="")
    _ = parser.add_argument("--tmux-window", default="")
    _ = parser.add_argument("--pane", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--tool", default=DEFAULT_TOOL, help="Worker CLI. Defaults to cursor; pass codex or pcodx to request those tools.")
    _ = parser.add_argument("--workdir", type=Path)
    _ = parser.add_argument("--window-name", default="")
    _ = parser.add_argument("--prompt-file", type=Path)
    _ = parser.add_argument("--no-link", action="store_true")
    _ = parser.add_argument("--dry-run", action="store_true", help="Print the planned launch or ownership migration without changing files or tmux.")
    _ = parser.add_argument("--session-id", default="", help="Codex session id to resume in a new worker window.")
    _ = parser.add_argument("--resume-idle", action="store_true", help="Resume --session-id without submitting a prompt.")
    _ = parser.add_argument("--model", default="", help="Model to use for a new worker launch.")
    _ = parser.add_argument("--reasoning-effort", choices=("low", "medium", "high", "xhigh", "max", "ultra"), default="", help="Start Codex with `model_reasoning_effort` for this worker.")
    _ = parser.add_argument("--codex-flag", action="append", help="Extra raw Codex argv token. Repeat for flags and values; use `--codex-flag=--flag` when the token starts with `--`.")
    _ = parser.add_argument("--manager-target", default="", help="Optional manager owner target to write as `managerat:` task metadata.")
    _ = parser.add_argument("--prelaunch-source", type=Path, help="Readable shell script to source before launching the worker command.")
    _ = parser.add_argument("--is-manager", action="store_true", help="Mark the task as a manager task in frontmatter.")
    _ = parser.add_argument("--human-email-file", type=Path, help="Email source file under ROOT/manager_mail; requires --human-email-lines.")
    _ = parser.add_argument("--human-email-lines", type=line_range, metavar="START-END", help="Inclusive email line range; requires --human-email-file.")
    _ = parser.add_argument(
        "--amh-caller-agent",
        default="",
        help="Optional AMH stable agent id to export as AMH_CALLER=agent:<id> for this launched Codex process.",
    )
    _ = parser.add_argument(
        "--require-existing-tmux-session",
        action="store_true",
        help="Refuse launch instead of creating the explicitly named tmux session when it is missing.",
    )
    _ = parser.add_argument(
        "--allow-new-tmux-session",
        action="store_true",
        help="Explicitly allow creating the named session when reuse is genuinely unsuitable.",
    )
    _ = parser.add_argument(
        "--migrate-manager-owner", action="store_true", help="Atomically migrate only `managerat` on one existing task; requires explicit old and new targets and performs no launch or TODO action."
    )
    _ = parser.add_argument("--old-manager-target", default="", help="Existing `managerat` value required by --migrate-manager-owner.")
    _ = parser.add_argument("--new-manager-target", default="", help="Replacement `managerat` value required by --migrate-manager-owner.")
    _ = parser.add_argument(
        "--prepared-successor-journal",
        type=Path,
        help="Committed omo_worker_successor.py journal authorizing one prepared blocked-worker launch.",
    )
    _ = parser.add_argument("--expected-prepared-journal-sha256", default="")
    _ = parser.add_argument("--expected-prepared-task-sha256", default="")
    _ = parser.add_argument("--expected-prepared-prompt-sha256", default="")
    _ = parser.add_argument("--expected-prepared-queue-sha256", default="")
    _ = parser.add_argument("--expected-prepared-launch-manifest-sha256", default="")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if not parsed.task_file.endswith(".md"):
        parser.error("--task-file must end with `.md`.")
    prepared_values = (
        parsed.prepared_successor_journal,
        parsed.expected_prepared_journal_sha256,
        parsed.expected_prepared_task_sha256,
        parsed.expected_prepared_prompt_sha256,
        parsed.expected_prepared_queue_sha256,
        parsed.expected_prepared_launch_manifest_sha256,
    )
    if any(prepared_values) and not all(prepared_values):
        parser.error("prepared-successor launch requires its journal plus exact journal, task, prompt, and queue SHA-256 values.")
    if parsed.pane:
        parser.error("pane selection is no longer supported; pane 0 is implied.")
    if parsed.tool not in COMMAND_BY_TOOL:
        parser.error("only --tool codex, --tool pcodx, or --tool cursor is supported.")
    tool_explicit = any(arg == "--tool" or arg.startswith("--tool=") for arg in argv)
    amh_caller_agent_explicit = any(arg == "--amh-caller-agent" or arg.startswith("--amh-caller-agent=") for arg in argv)
    if (parsed.human_email_file is None) != (parsed.human_email_lines is None):
        parser.error("--human-email-file and --human-email-lines must be supplied together.")
    if not parsed.migrate_manager_owner and (parsed.old_manager_target or parsed.new_manager_target):
        parser.error("--old-manager-target and --new-manager-target require --migrate-manager-owner.")
    if parsed.migrate_manager_owner:
        if not parsed.old_manager_target or not parsed.new_manager_target:
            parser.error("--migrate-manager-owner requires --old-manager-target OLD and --new-manager-target NEW.")
        if any(
            (
                parsed.tmux_session,
                parsed.tmux_window,
                parsed.workdir,
                parsed.window_name,
                parsed.prompt_file,
                parsed.no_link,
                parsed.session_id,
                parsed.resume_idle,
                parsed.model,
                parsed.reasoning_effort,
                parsed.codex_flag,
                tool_explicit,
                parsed.manager_target,
                parsed.prelaunch_source,
                parsed.is_manager,
                parsed.human_email_file,
                parsed.human_email_lines,
                parsed.amh_caller_agent,
                parsed.require_existing_tmux_session,
                parsed.allow_new_tmux_session,
                parsed.prepared_successor_journal,
                parsed.expected_prepared_journal_sha256,
                parsed.expected_prepared_task_sha256,
                parsed.expected_prepared_prompt_sha256,
                parsed.expected_prepared_queue_sha256,
                parsed.expected_prepared_launch_manifest_sha256,
            )
        ):
            parser.error("--migrate-manager-owner only accepts --root, --task-file, explicit old/new manager targets, and optional --dry-run.")
    if not parsed.migrate_manager_owner and not parsed.tmux_session:
        parser.error("--tmux-session is required.")
    if parsed.require_existing_tmux_session and parsed.allow_new_tmux_session:
        parser.error("--require-existing-tmux-session and --allow-new-tmux-session are mutually exclusive.")
    if parsed.tmux_session and TMUX_SESSION_RE.fullmatch(parsed.tmux_session) is None:
        parser.error("--tmux-session must be an exact session name starting with a letter and containing only letters, numbers, `_`, or `-`.")
    if parsed.resume_idle and not parsed.session_id:
        parser.error("--resume-idle requires --session-id.")
    if parsed.resume_idle and parsed.workdir is None:
        parser.error("--resume-idle requires --workdir.")
    if parsed.resume_idle and parsed.prompt_file is not None:
        parser.error("--resume-idle does not accept --prompt-file.")
    if parsed.resume_idle and parsed.human_email_file is not None:
        parser.error("--resume-idle does not accept human email instructions.")
    if amh_caller_agent_explicit and AMH_AGENT_ID_RE.fullmatch(parsed.amh_caller_agent) is None:
        parser.error("--amh-caller-agent must be a nonempty ASCII AMH agent id using only letters, numbers, `.`, `_`, or `-`.")
    if parsed.amh_caller_agent and parsed.workdir is None:
        parser.error("--amh-caller-agent is only valid for a launched worker with --workdir.")
    if parsed.human_email_file is not None and parsed.workdir is None:
        parser.error("--human-email-file and --human-email-lines require --workdir.")
    if parsed.human_email_file is not None and parsed.prompt_file is None:
        parser.error("--human-email-file and --human-email-lines require --prompt-file.")
    if parsed.prepared_successor_journal is not None:
        if parsed.prompt_file is None or parsed.workdir is None:
            parser.error("prepared-successor launch requires --prompt-file and --workdir.")
        if parsed.no_link is False:
            parser.error("prepared-successor launch requires --no-link because TODO was committed by the preparation transaction.")
        if parsed.is_manager or parsed.session_id or parsed.resume_idle or parsed.human_email_file is not None:
            parser.error("prepared-successor launch is only for a fresh ordinary worker without Human email input.")
        if parsed.manager_target == "":
            parser.error("prepared-successor launch requires the exact --manager-target.")
        if not parsed.tmux_window:
            parser.error("prepared-successor launch requires the exact --tmux-window.")
        if not parsed.require_existing_tmux_session or parsed.allow_new_tmux_session:
            parser.error("prepared-successor launch requires an existing non-Human tmux session and cannot create a session.")
        for value in (
            parsed.expected_prepared_journal_sha256,
            parsed.expected_prepared_task_sha256,
            parsed.expected_prepared_prompt_sha256,
            parsed.expected_prepared_queue_sha256,
            parsed.expected_prepared_launch_manifest_sha256,
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                parser.error("prepared-successor SHA-256 values must be lowercase hexadecimal digests.")
    if parsed.workdir is not None and not parsed.resume_idle and (not parsed.model.strip() or not parsed.reasoning_effort.strip()):
        parser.error("--workdir requires nonempty --model MODEL and --reasoning-effort EFFORT.")
    if invalid_model := model_error(parsed.model):
        parser.error(invalid_model)
    raw_model_flag_error = codex_flags_model_error(tuple(parsed.codex_flag or ()))
    if raw_model_flag_error:
        parser.error(raw_model_flag_error)
    if parsed.tool == "cursor" and parsed.codex_flag:
        parser.error("--codex-flag is only valid for Codex tools.")
    prelaunch_source = parsed.prelaunch_source.resolve() if parsed.prelaunch_source is not None else None
    return Args(
        parsed.root.resolve(),
        parsed.task_file,
        parsed.tmux_session,
        parsed.tmux_window,
        parsed.tool,
        parsed.workdir.resolve() if parsed.workdir is not None else None,
        parsed.window_name,
        parsed.prompt_file,
        parsed.no_link,
        parsed.dry_run,
        parsed.session_id,
        parsed.reasoning_effort,
        tuple(parsed.codex_flag or ()),
        tool_explicit,
        parsed.manager_target,
        prelaunch_source,
        parsed.is_manager,
        parsed.migrate_manager_owner,
        parsed.old_manager_target,
        parsed.new_manager_target,
        model=parsed.model,
        human_email_file=parsed.human_email_file,
        human_email_lines=parsed.human_email_lines,
        resume_idle=parsed.resume_idle,
        amh_caller_agent=parsed.amh_caller_agent,
        require_existing_tmux_session=parsed.require_existing_tmux_session,
        allow_new_tmux_session=parsed.allow_new_tmux_session,
        prepared_successor_journal=parsed.prepared_successor_journal.expanduser().resolve(strict=False)
        if parsed.prepared_successor_journal is not None
        else None,
        expected_prepared_journal_sha256=parsed.expected_prepared_journal_sha256,
        expected_prepared_task_sha256=parsed.expected_prepared_task_sha256,
        expected_prepared_prompt_sha256=parsed.expected_prepared_prompt_sha256,
        expected_prepared_queue_sha256=parsed.expected_prepared_queue_sha256,
        expected_prepared_launch_manifest_sha256=parsed.expected_prepared_launch_manifest_sha256,
    )


def task_path(root: Path, task_file: str) -> Path:
    path = (root / task_file).resolve(strict=False)
    if root not in path.parents and path != root:
        raise ValueError("task file escapes root")
    return path


def task_ref(root: Path, task_file: str) -> str:
    return task_path(root, task_file).relative_to(root.resolve()).as_posix()


def canonical_tmux_pane(tmux_target: str) -> tuple[str, int, int]:
    session, window_and_pane = tmux_target.split(":", 1)
    window, dot, pane = window_and_pane.partition(".")
    return session, int(window), int(pane) if dot else 0


def canonical_tmux_pane_text(tmux_target: str) -> str:
    session, window, pane = canonical_tmux_pane(tmux_target)
    return f"{session}:{window}.{pane}"


def migration_source_metadata(text: str, work_log_root: Path | None = None):
    """Parse migration input while permitting the legacy self-owner defect it repairs."""

    try:
        return parse_task_metadata(text, work_log_root)
    except TaskFrontmatterError as exc:
        if str(exc) != "`managerat` must be different from `runat`.":
            raise
        return None


def manager_owner_migration_text(text: str, old_owner: str, new_owner: str, work_log_root: Path | None = None) -> str:
    """Return valid task text with only the exact frontmatter owner value changed."""
    metadata = migration_source_metadata(text, work_log_root)
    for label, owner in (("old", old_owner), ("new", new_owner)):
        if TMUX_TARGET_RE.fullmatch(owner) is None:
            raise ValueError(f"{label} manager target must be a full tmux target like `SESSION:WINDOW`.")
    if canonical_tmux_pane(old_owner) == canonical_tmux_pane(new_owner):
        raise ValueError("old and new manager targets must identify different tmux panes.")
    if metadata is not None and metadata.version == TASK_FRONTMATTER_V2:
        if canonical_tmux_pane(metadata.runat) == canonical_tmux_pane(new_owner):
            raise ValueError("new manager target must be different from task `runat`.")
        frontmatter, body = split_task_text(text)
        values = load_yaml_mapping(frontmatter)
        if values["managerat"] != old_owner:
            raise ValueError(f"existing managerat {values['managerat']} does not equal --old-manager-target {old_owner}.")
        values["managerat"] = new_owner
        return render_task(values, body)
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("ownership migration requires an existing task with valid frontmatter.")
    try:
        closing_idx = next(idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("ownership migration requires an existing task with valid frontmatter.") from exc
    owner_indexes = [idx for idx, line in enumerate(lines[1:closing_idx], start=1) if line.rstrip("\r\n").partition(":")[0] == "managerat"]
    if len(owner_indexes) != 1:
        raise ValueError("ownership migration requires exactly one frontmatter `managerat` field.")
    runat_values = [line.rstrip("\r\n").partition(":")[2].strip() for line in lines[1:closing_idx] if line.rstrip("\r\n").partition(":")[0] == "runat"]
    if any(TMUX_TARGET_RE.fullmatch(runat) is not None and canonical_tmux_pane(new_owner) == canonical_tmux_pane(runat) for runat in runat_values):
        raise ValueError("new manager target must be different from task `runat`.")
    owner_idx = owner_indexes[0]
    line = lines[owner_idx]
    content = line.rstrip("\r\n")
    line_ending = line[len(content) :]
    key, separator, value = content.partition(":")
    existing_owner = value.strip()
    if existing_owner != old_owner:
        raise ValueError(f"existing managerat {existing_owner} does not equal --old-manager-target {old_owner}.")
    value_start = len(value) - len(value.lstrip())
    value_end = len(value.rstrip())
    lines[owner_idx] = f"{key}{separator}{value[:value_start]}{new_owner}{value[value_end:]}{line_ending}"
    updated = "".join(lines)
    try:
        updated_metadata = parse_task_metadata(updated, work_log_root)
    except TaskFrontmatterError as exc:
        if str(exc) == "`managerat` must be different from `runat`.":
            raise ValueError("new manager target must be different from task `runat`.") from exc
        raise
    if updated_metadata is None or updated_metadata.managerat != new_owner:
        raise RuntimeError("updated task frontmatter did not retain the requested manager owner.")
    return updated


def same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and left.st_mtime_ns == right.st_mtime_ns and left.st_size == right.st_size


def same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def atomic_replace_if_unchanged(path: Path, text: str, before: os.stat_result, *, lock_held: bool = False) -> None:
    """Atomically replace `path` only if it still matches the state that was read."""
    tmp_path: Path | None = None
    lock_context = contextlib.nullcontext() if lock_held else task_file_lock(path)
    with lock_context:
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
                tmp_path = Path(handle.name)
                _ = handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.chmod(before.st_mode & 0o7777)
            if not same_file_state(before, path.stat()):
                raise ValueError("task file changed while ownership migration was being prepared; retry after rereading it.")
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)


def migrate_manager_owner(
    path: Path,
    old_owner: str,
    new_owner: str,
    dry_run_only: bool = False,
    work_log_root: Path | None = None,
) -> None:
    """Validate and migrate one existing task owner without touching other state."""
    if not path.is_file():
        raise ValueError(f"ownership migration requires an existing task file: {path}")
    membership_root = (work_log_root or infer_work_log_root(path)).resolve(strict=False)
    try:
        path.resolve(strict=False).relative_to(membership_root)
    except ValueError as exc:
        raise ValueError("ownership migration task must be inside the authoritative work-log root.") from exc
    while True:
        with root_membership_lock(membership_root), path.open("r", encoding="utf-8", newline="") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            before = os.fstat(handle.fileno())
            if not same_file_identity(before, path.stat()):
                continue
            existing = handle.read()
            _ = migration_source_metadata(existing, membership_root)
            updated = manager_owner_migration_text(existing, old_owner, new_owner, membership_root)
            if dry_run_only:
                print(f"dry-run: would change only managerat from {old_owner} to {new_owner} in {path}; no files or tmux panes changed.")
                return
            atomic_replace_if_unchanged(path, updated, before)
            print(f"migrated only managerat from {old_owner} to {new_owner} in {path}")
            return


def target(args: Args) -> str:
    if args.tmux_session and args.tmux_window:
        return f"{args.tmux_session}:{args.tmux_window}"
    return args.tmux_session


def current_manager_target() -> str:
    for key in ("OMO_MANAGER_TMUX_TARGET", "OMO_AGENT_TMUX_TARGET"):
        target = os.environ.get(key, "").strip()
        if TMUX_TARGET_RE.fullmatch(target) is not None:
            return target
    if "TMUX" not in os.environ:
        return ""
    result = subprocess.run(["tmux", "display-message", "-p", "#S:#I"], capture_output=True, text=True, timeout=10, check=False)
    target = result.stdout.strip() if result.returncode == 0 else ""
    return target if TMUX_TARGET_RE.fullmatch(target) is not None else ""


def frontmatter_body_line_index(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return idx + 1
    return 0


def target_session(tmux_target: str) -> str:
    return tmux_target.split(":", 1)[0]


def is_human_tmux_session(tmux_target: str) -> bool:
    """Return whether a target belongs to the human-only `h*` namespace."""

    return target_session(tmux_target).startswith("h")


def resolved_launch_session_name(session_target: str, client_args: Args | None = None) -> str:
    """Resolve the exact requested session before applying the human-session boundary."""

    if client_args is None:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"={session_target}:", "#S"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    else:
        result = tmux_for_args(client_args, ["display-message", "-p", "-t", f"={session_target}:", "#S"])
    resolved = result.stdout.strip() if result.returncode == 0 else ""
    return resolved or session_target


def human_authorized_launch_session(args: Args, session_name: str) -> bool:
    """Require one direct human launch request naming the exact `h*` session."""

    if args.human_email_file is None or args.human_email_lines is None:
        return False
    excerpt = human_email_excerpt(args)
    session_re = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(session_name)}(?=$|[^A-Za-z0-9_-])")
    for line in excerpt.splitlines():
        negation_text = HUMAN_LAUNCH_DISCOURSE_NEGATION_RE.sub("", line, count=1)
        if HUMAN_LAUNCH_REQUEST_RE.match(line) is None or HUMAN_LAUNCH_NEGATION_RE.search(negation_text) is not None or session_re.search(line) is None:
            continue
        role = HUMAN_LAUNCH_ROLE_RE.search(line)
        role_target = HUMAN_LAUNCH_ROLE_TARGET_RE.search(line, role.end()) if role is not None else None
        if role_target is not None and role_target.group(1) == session_name:
            return True
        direct_target = HUMAN_DIRECT_LAUNCH_TARGET_RE.match(line)
        if role_target is None and direct_target is not None and direct_target.group(1) == session_name:
            return True
    return False


def human_authorized_create_session(args: Args, session_name: str) -> bool:
    """Require a separate exact imperative creating the named `h*` session."""

    if args.human_email_file is None or args.human_email_lines is None:
        return False
    create_re = re.compile(
        rf"\s*(?:please\s+)?(?:create|make|set\s+up|start)\s+(?:(?:a|the)\s+)?(?:new\s+)?(?:tmux\s+)?session\s+(?:named\s+)?[`'\"]?{re.escape(session_name)}[`'\"]?\s*[.!]?\s*",
        flags=re.IGNORECASE,
    )
    for line in human_email_excerpt(args).splitlines():
        negation_text = HUMAN_LAUNCH_DISCOURSE_NEGATION_RE.sub("", line, count=1)
        if HUMAN_LAUNCH_NEGATION_RE.search(negation_text) is None and create_re.fullmatch(line) is not None:
            return True
    return False


def validate_launch_session(args: Args) -> str:
    """Resolve and authorize the tmux session used for a new window."""

    session_name = resolved_launch_session_name(args.tmux_session, args if args.prepared_runtime_path is not None else None)
    if session_name.startswith("h") and not human_authorized_launch_session(args, session_name):
        raise ValueError("launches in human-owned `h*` tmux sessions require an authoritative direct launch request naming that exact session.")
    return session_name


def launch_session(args: Args) -> LaunchSession:
    """Bind an existing session or prepare one explicitly named session for creation."""

    if args.workdir is None:
        raise ValueError("--workdir is required to launch a new worker.")
    session_name = validate_launch_session(args)
    exact_session_target = f"={session_name}:"
    result = tmux_for_args(args if args.prepared_runtime_path is not None else None, ["display-message", "-p", "-t", exact_session_target, "#{session_id}"])
    # tmux 3.4 may return success with an empty format expansion when an exact
    # missing-session target such as `=name:` is queried.  Treat only that
    # empty result like the ordinary nonzero "missing session" response;
    # nonempty malformed identities still fail closed below.
    if result.returncode != 0 or not result.stdout.strip():
        if not args.allow_new_tmux_session:
            raise ValueError(
                f"tmux session `{session_name}` must already exist; reuse an existing non-human session, "
                "or pass --allow-new-tmux-session only when a new session is genuinely needed."
            )
        if args.tmux_window:
            raise ValueError(f"cannot create missing tmux session `{session_name}` at requested --tmux-window {args.tmux_window}.")
        if session_name.startswith("h") and not human_authorized_create_session(args, session_name):
            raise ValueError("creating a human-owned `h*` tmux session requires authoritative direct text explicitly creating that exact session.")
        print(
            f"warning: explicitly creating tmux session `{session_name}`; reuse an existing non-human session when practical.",
            file=sys.stderr,
        )
        return LaunchSession(session_name, "", True)
    if session_name.startswith("h") and not human_authorized_launch_session(args, session_name):
        raise ValueError("launches in an existing human-owned `h*` tmux session require an authoritative direct launch request naming that exact session.")
    session_id = result.stdout.strip()
    if TMUX_SESSION_ID_RE.fullmatch(session_id) is None:
        raise RuntimeError(f"tmux session `{session_name}` did not report one usable session_id.")
    return LaunchSession(session_name, session_id, False)


def is_vl_task_file(task_file: str) -> bool:
    return Path(task_file).name.startswith("vl_")


def is_vl_submanager_task_file(task_file: str) -> bool:
    name = Path(task_file).name
    return name.startswith("vl_submanager_current_") or name.startswith("vl_supervisor_current_")


def is_vl_agent(task_file: str, tmux_target: str) -> bool:
    return is_vl_task_file(task_file) or target_session(tmux_target) == "vl"


def header(tmux_target: str, tool: str) -> str:
    return f"runat: {tmux_target} {tool}" if tmux_target else ""


def target_aliases(tmux_target: str) -> set[str]:
    aliases = {tmux_target} if tmux_target else set()
    window_target, dot, _pane = tmux_target.rpartition(".")
    if dot and ":" in window_target:
        aliases.add(window_target)
    elif tmux_target and not dot:
        aliases.add(f"{tmux_target}.0")
    return aliases


def upsert_header(existing: str, first: str) -> str:
    if not first:
        return existing
    if not existing:
        return f"{first}\n"
    lines = existing.splitlines(keepends=True)
    if lines and is_runat_header(lines[0]):
        lines[0] = f"{first}\n"
        return "".join(lines)
    return f"{first}\n\n{existing}"


def first_non_metadata_index(lines: list[str]) -> int:
    idx = frontmatter_body_line_index(lines)
    if idx < len(lines) and is_runat_header(lines[idx]):
        idx += 1
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped:
            idx += 1
            continue
        if any(stripped.startswith(prefix) for prefix in TASK_METADATA_PREFIXES):
            idx += 1
            continue
        break
    return idx


def managerat_line_error(line: str) -> str:
    stripped = line.strip()
    parts = stripped.split()
    if stripped.startswith("managerat:") and (len(parts) != 2 or parts[0] != "managerat:"):
        return "task `managerat:` metadata must be exactly `managerat: TARGET`."
    return ""


def managerat_value(text: str) -> str:
    lines = text.splitlines()
    for line in lines[: first_non_metadata_index(lines)]:
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "managerat:":
            return parts[1]
    return ""


def validate_managerat_metadata(text: str) -> None:
    lines = text.splitlines()
    for line in lines[: first_non_metadata_index(lines)]:
        if error := managerat_line_error(line):
            raise ValueError(error)


def upsert_managerat(text: str, manager_target: str) -> str:
    if not manager_target:
        return text
    lines = text.splitlines(keepends=True)
    metadata_end = first_non_metadata_index([line.rstrip("\n") for line in lines])
    for idx, line in enumerate(lines[:metadata_end]):
        if error := managerat_line_error(line):
            raise ValueError(error)
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "managerat:":
            lines[idx] = f"managerat: {manager_target}\n"
            return "".join(lines)
    insert_at = 1 if lines and is_runat_header(lines[0]) else 0
    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] = f"{lines[insert_at - 1]}\n"
    lines.insert(insert_at, f"managerat: {manager_target}\n")
    return "".join(lines)


def is_bullet(line: str) -> bool:
    stripped = line.lstrip()
    return any(stripped.startswith(marker) for marker in BULLET_MARKERS)


def is_runat_header(line: str) -> bool:
    return line.strip().split(maxsplit=1)[0:1] == ["runat:"]


def runat_header_error(text: str) -> str:
    lines = text.splitlines()
    if not lines or not is_runat_header(lines[0]):
        return ""
    parts = lines[0].strip().split()
    if len(parts) != 3 or parts[2] not in COMMAND_BY_TOOL:
        return "task files starting with `runat:` must keep the first line exactly `runat: TARGET TOOL`."
    return ""


def validate_runat_header(text: str) -> None:
    if error := runat_header_error(text):
        raise ValueError(error)


def has_pending_task_items_marker(text: str) -> bool:
    return any(line.strip() == PENDING_TASK_ITEMS_MARKER for line in text.splitlines())


def insert_pending_task_items_marker(text: str) -> str:
    if has_pending_task_items_marker(text):
        return text

    lines = text.splitlines(keepends=True)
    goal_idx = first_goal_line_index([line.rstrip("\n") for line in lines])
    insert_idx = min(goal_idx + 1, len(lines))
    while insert_idx < len(lines) and is_bullet(lines[insert_idx]):
        insert_idx += 1
    lines.insert(insert_idx, f"{PENDING_TASK_ITEMS_MARKER}\n")
    return "".join(lines)


def first_goal_line_index(lines: list[str]) -> int:
    idx = frontmatter_body_line_index(lines)
    if idx < len(lines) and lines[idx].strip().startswith("<manager_delegation "):
        idx += 1
    if idx < len(lines) and is_runat_header(lines[idx]):
        idx += 1
    while idx < len(lines) and any(lines[idx].strip().startswith(prefix) for prefix in TASK_METADATA_PREFIXES):
        idx += 1
    return idx


def runat_goal_tree_error(text: str) -> str:
    lines = text.splitlines()
    if not lines or (frontmatter_body_line_index(lines) == 0 and not is_runat_header(lines[0])):
        return ""
    goal_idx = first_goal_line_index(lines)
    if len(lines) <= goal_idx or not lines[goal_idx].strip():
        return "task files starting with `runat:` must put a high-level goal directly after the `runat:` line."
    if is_bullet(lines[goal_idx]):
        return "task files starting with `runat:` must use a plain high-level goal line before bullet subgoals."
    subgoal_idx = goal_idx + 1
    while subgoal_idx < len(lines) and not lines[subgoal_idx].strip():
        subgoal_idx += 1
    if len(lines) <= subgoal_idx or not is_bullet(lines[subgoal_idx]):
        return "task files starting with `runat:` must put at least one concrete bullet subgoal directly under the high-level goal."
    return ""


def validate_runat_goal_tree(text: str) -> None:
    if error := runat_goal_tree_error(text):
        raise ValueError(error)


def top_header_tool(text: str) -> str:
    first = text.splitlines()[0].strip().split() if text.splitlines() else []
    if len(first) >= 3 and first[0] == "runat:" and first[-1] in COMMAND_BY_TOOL:
        return first[-1]
    return ""


def runat_entries(text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[0] == "runat:" and parts[-1] in COMMAND_BY_TOOL:
            entries.append((parts[1], parts[-1]))
    return entries


def task_file_tool(text: str, tmux_target: str) -> str:
    if tool := top_header_tool(text):
        return tool
    entries = runat_entries(text)
    aliases = target_aliases(tmux_target)
    for entry_target, tool in reversed(entries):
        if entry_target in aliases:
            return tool
    if entries:
        return entries[-1][1]
    return ""


def effective_tool(args: Args) -> str:
    return args.tool


def managerat_for_task(args: Args, runat: str) -> str:
    managerat = args.manager_target.strip() or current_manager_target()
    if not managerat:
        raise ValueError("--manager-target or OMO_AGENT_TMUX_TARGET is required to write task frontmatter.")
    if TMUX_TARGET_RE.fullmatch(managerat) is None:
        raise ValueError("task frontmatter `managerat` must be a tmux target.")
    if managerat in target_aliases(runat):
        raise ValueError("task frontmatter `managerat` must be different from `runat`.")
    return managerat


def task_frontmatter(args: Args, runat: str, managerat: str) -> str:
    is_manager = "true" if args.is_manager else "false"
    status = "long_running" if args.is_manager else "running"
    if v2_enabled(args.root):
        blocked_on = [{"kind": "persistent", "reason": DEFAULT_LONG_RUNNING_BLOCKED_ON}] if args.is_manager else []
        rendered = render_task(
            {
                "version": V2_VERSION,
                "task_id": generated_id("task"),
                "status": status,
                "runat": runat,
                "tool": effective_tool(args),
                "managerat": managerat,
                "is_manager": args.is_manager,
                **({"blocked_on": blocked_on} if blocked_on else {}),
                "pending_task_items": [],
                "resolved_task_items": [],
            },
            "",
            args.root,
        )
        return rendered.removesuffix("\n")
    return "\n".join(
        [
            "---",
            f"version: {TASK_FRONTMATTER_VERSION}",
            f"status: {status}",
            *([f"blocked_on: {DEFAULT_LONG_RUNNING_BLOCKED_ON}"] if args.is_manager else []),
            f"runat: {runat}",
            f"tool: {effective_tool(args)}",
            f"managerat: {managerat}",
            f"is_manager: {is_manager}",
            "pending_task_items: []",
            "---",
        ]
    )


def replace_frontmatter_fields(text: str, updates: dict[str, str], remove: set[str] | None = None) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    remove = remove or set()
    for closing_idx, line in enumerate(lines[1:], start=1):
        if line.strip() != "---":
            continue
        body = lines[1:closing_idx]
        updated: list[str] = [lines[0]]
        pending_updates = dict(updates)
        for item in body:
            key, sep, _value = item.partition(":")
            if sep and key in remove:
                continue
            if sep and key in pending_updates:
                updated.append(f"{key}: {pending_updates.pop(key)}\n")
                continue
            updated.append(item)
        for key, value in pending_updates.items():
            updated.append(f"{key}: {value}\n")
        updated.extend(lines[closing_idx:])
        return "".join(updated)
    return text


def launched_frontmatter_text(existing: str, args: Args, tmux_target: str) -> str:
    metadata = parse_task_metadata(existing, args.root)
    is_manager = args.is_manager or (metadata is not None and metadata.is_manager)
    is_long_running = is_manager or (metadata is not None and (metadata.status == "long_running" or metadata.resume_status == "long_running"))
    if metadata is not None and metadata.version == V2_VERSION:
        frontmatter, body = split_task_text(existing)
        values = load_yaml_mapping(frontmatter)
        desired_status = "long_running" if is_long_running else "running"
        blockers = values.get("blocked_on", [])
        generated = [blocker for blocker in blockers if blocker.get("kind") == "pending_items"]
        persistent = [blocker for blocker in blockers if blocker.get("kind") == "persistent"]
        external = [blocker for blocker in blockers if blocker.get("kind") not in {"pending_items", "persistent"}]
        values["runat"] = tmux_target
        values["tool"] = effective_tool(args)
        if args.manager_target:
            values["managerat"] = args.manager_target
        if args.is_manager:
            values["is_manager"] = True
        if generated or external:
            values["status"] = "blocked"
            values["resume_status"] = desired_status
            values["blocked_on"] = [*generated, *persistent, *external]
        else:
            values["status"] = desired_status
            values.pop("resume_status", None)
            if is_long_running and persistent:
                values["blocked_on"] = persistent
            else:
                values.pop("blocked_on", None)
        return render_task(values, body, args.root)
    updates = {
        "status": "long_running" if is_long_running else "running",
        "runat": tmux_target,
        "tool": effective_tool(args),
    }
    if args.manager_target:
        updates["managerat"] = args.manager_target
    if args.is_manager:
        updates["is_manager"] = "true"
    if is_long_running and metadata is not None and metadata.status == "long_running" and metadata.blocked_on:
        updates["blocked_on"] = metadata.blocked_on
    return replace_frontmatter_fields(existing, updates, set() if "blocked_on" in updates else {"blocked_on"})


def new_task_text(args: Args, tmux_target: str, validate_target: bool = True) -> str:
    if not tmux_target:
        raise ValueError("runat tmux target is required to write task frontmatter.")
    if validate_target and TMUX_TARGET_RE.fullmatch(tmux_target) is None:
        raise ValueError("runat tmux target must be a full tmux target like `SESSION:WINDOW`.")
    managerat = managerat_for_task(args, tmux_target)
    body = task_instruction_text(args, managerat)
    return f"{task_frontmatter(args, tmux_target, managerat)}\n{body}\n"


def readable_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise ValueError(f"{label} file not found: {path}")
    if not os.access(path, os.R_OK):
        raise ValueError(f"{label} file is not readable: {path}")


def human_email_path(args: Args) -> Path | None:
    if args.human_email_file is None:
        return None
    mail_root = (args.root / "manager_mail").resolve()
    candidate = args.human_email_file
    path = (candidate if candidate.is_absolute() else args.root / candidate).resolve()
    if path == mail_root or mail_root not in path.parents:
        raise ValueError("human email file must resolve inside ROOT/manager_mail.")
    return path


def human_email_excerpt(args: Args) -> str:
    if args.human_email_text is not None:
        return args.human_email_text
    path = human_email_path(args)
    if path is None or args.human_email_lines is None:
        return ""
    readable_file(path, "human email")
    with path.open("r", encoding="utf-8", newline="") as source:
        lines = source.read().splitlines(keepends=True)
    start, end = args.human_email_lines
    if end > len(lines):
        raise ValueError(f"human email line range ends at {end}, but the file has only {len(lines)} lines.")
    excerpt = "".join(lines[start - 1 : end])
    if HUMAN_INSTRUCTION_CLOSE in excerpt.casefold():
        raise ValueError(f"human email excerpt must not contain {HUMAN_INSTRUCTION_CLOSE} in any letter case.")
    return excerpt


def human_email_source(args: Args) -> str:
    path = human_email_path(args)
    if path is None or args.human_email_lines is None:
        return ""
    start, end = args.human_email_lines
    relative = path.relative_to(args.root.resolve()).as_posix()
    return f"{relative}:{start}-{end}"


def authoritative_human_instruction(excerpt: str, source: str = "") -> str:
    source_attr = f' source="{html.escape(source, quote=True)}"' if source else ""
    return f'<human_instruction authoritative="true"{source_attr}>\n{excerpt}{HUMAN_INSTRUCTION_CLOSE}'


def manager_delegation(prompt: str, source_target: str) -> str:
    lowered = prompt.casefold()
    if MANAGER_DELEGATION_CLOSE in lowered:
        raise ValueError(f"manager prompt must not contain {MANAGER_DELEGATION_CLOSE} in any letter case.")
    if HUMAN_INSTRUCTION_OPEN in lowered:
        raise ValueError(f"manager prompt must not contain {HUMAN_INSTRUCTION_OPEN} in any letter case.")
    source = html.escape(source_target, quote=True)
    return f'<manager_delegation from="{source}">\n{prompt.rstrip()}\n{MANAGER_DELEGATION_CLOSE}'


def task_instruction_text(args: Args, manager_target: str) -> str:
    parts: list[str] = []
    if args.prompt_file is not None:
        parts.append(manager_delegation(args.prompt_file.read_text(encoding="utf-8"), manager_target))
    excerpt = human_email_excerpt(args)
    if excerpt:
        parts.append(authoritative_human_instruction(excerpt, human_email_source(args)))
    return "\n".join(parts)


def write_instruction_file(text: str, prefix: str) -> Path:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", prefix=prefix, delete=False) as handle:
        os.fchmod(handle.fileno(), 0o600)
        _ = handle.write(f"\n{text}")
        return Path(handle.name)


def write_human_instruction_file(excerpt: str, source: str = "") -> Path:
    return write_instruction_file(authoritative_human_instruction(excerpt, source), "omo-human-instruction.")


def write_manager_delegation_file(prompt_file: Path, source_target: str) -> Path:
    return write_instruction_file(manager_delegation(prompt_file.read_text(encoding="utf-8"), source_target), "omo-manager-delegation.")


def prompt_input(
    prompt_file: Path | None,
    vl_agent: bool = False,
    manager_file: Path | None = None,
    human_instruction_file: Path | None = None,
) -> str:
    paths = [DEFAULT_WORKER_INSTRUCTIONS]
    if vl_agent:
        paths.append(VL_WORKER_INSTRUCTIONS)
    if manager_file is not None:
        paths.append(manager_file)
    if prompt_file is not None:
        paths.append(prompt_file)
    if human_instruction_file is not None:
        paths.append(human_instruction_file)
    quoted_paths = " ".join(shlex.quote(str(path)) for path in paths)
    return f'"$(cat -- {quoted_paths})"'


def codex_cmd(
    session_id: str = "",
    reasoning_effort: str = "",
    codex_flags: tuple[str, ...] = (),
    prompt_file: Path | None = None,
    tool: str = DEFAULT_TOOL,
    vl_agent: bool = False,
    model: str = "",
    manager_file: Path | None = None,
    human_instruction_file: Path | None = None,
    include_prompt: bool = True,
    workdir: Path | None = None,
    cursor_runtime: Path | None = None,
) -> str:
    if tool == "cursor" and cursor_runtime is not None:
        args = [str(cursor_runtime), *COMMAND_BY_TOOL[tool][1:]]
    else:
        try:
            args = list(COMMAND_BY_TOOL[tool])
        except KeyError as exc:
            raise ValueError(f"unsupported tool: {tool}") from exc
    if tool == "cursor":
        if codex_flags:
            raise ValueError("codex flags are not valid for Cursor Agent CLI.")
        if workdir is not None:
            args.extend(("--workspace", str(workdir)))
        if model:
            cursor_model = f"{model}-{reasoning_effort}" if reasoning_effort else model
            args.extend(("--model", cursor_model))
        args.extend(codex_flags)
        if session_id:
            args.extend(("--resume", session_id))
        parts = [shlex.quote(arg) for arg in args]
        if include_prompt:
            parts.append(prompt_input(prompt_file, vl_agent, manager_file, human_instruction_file))
        return " ".join(parts)
    if model:
        args.extend(("--model", model))
    if reasoning_effort:
        args.extend(("--config", f'model_reasoning_effort="{reasoning_effort}"'))
    args.extend(codex_flags)
    if session_id and tool == "codex" and workdir is not None:
        args.extend(("--cd", str(workdir)))
    if session_id:
        args.extend(("resume", session_id))
    parts = [shlex.quote(arg) for arg in args]
    if include_prompt:
        parts.append(prompt_input(prompt_file, vl_agent, manager_file, human_instruction_file))
    return " ".join(parts)


def shell_cmd(command: str) -> str:
    return "bash -lc " + shlex.quote(command)


def worker_command(
    command: str,
    tmux_target: str,
    prelaunch_source: Path | None = None,
    launch_marker: str = "",
    amh_caller_agent: str = "",
) -> str:
    exports = {"OMO_AGENT_TMUX_TARGET": tmux_target}
    if amh_caller_agent:
        if AMH_AGENT_ID_RE.fullmatch(amh_caller_agent) is None:
            raise ValueError("AMH caller agent id is invalid.")
        exports["AMH_CALLER"] = f"agent:{amh_caller_agent}"
    export_text = " ".join(f"{key}={shlex.quote(value)}" for key, value in exports.items())
    marker = f" && printf '%s\\n' {shlex.quote(launch_marker)}" if launch_marker else ""
    launch = f"export {export_text}{marker} && exec {command}"
    if prelaunch_source is None:
        return launch
    return f"source {shlex.quote(str(prelaunch_source))} && {launch}"


def prepared_shell_launch_command(command: str, tmux_target: str, args: Args, launch_marker: str) -> str:
    """Build one env-empty, absolute-runtime prepared Cursor shell command."""

    if args.prepared_shell_path is None or args.prepared_env_path is None or not args.prepared_launch_environment:
        raise RuntimeError("prepared launch lost its pinned shell/environment binding")
    environment = [f"{key}={value}" for key, value in args.prepared_launch_environment]
    environment.append(f"OMO_AGENT_TMUX_TARGET={tmux_target}")
    if args.amh_caller_agent:
        environment.append(f"AMH_CALLER=agent:{args.amh_caller_agent}")
    inner = f"printf '%s\\n' {shlex.quote(launch_marker)} && exec {command}"
    return "exec " + shlex.join(
        [str(args.prepared_env_path), "-i", *environment, str(args.prepared_shell_path), "--noprofile", "--norc", "-c", inner]
    )


def new_launch_marker() -> str:
    return f"{CODEX_LAUNCH_MARKER_PREFIX}{uuid.uuid4().hex}]"


def tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10, check=check)


def prepared_tmux_client(args: Args) -> tuple[Path, dict[str, str]]:
    """Return the exact manifest-bound tmux client and its env-empty environment."""

    if args.prepared_tmux_path is None or not args.prepared_tmux_environment:
        raise RuntimeError("prepared launch lost its pinned tmux client/environment binding")
    return args.prepared_tmux_path, dict(args.prepared_tmux_environment)


def tmux_for_args(args: Args | None, command: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    """Use the ordinary client, or the pinned sanitized client for a prepared launch."""

    if args is None or args.prepared_runtime_path is None:
        return tmux(command, check=check)
    executable, environment = prepared_tmux_client(args)
    return subprocess.run(
        [str(executable), *command],
        capture_output=True,
        text=True,
        timeout=10,
        check=check,
        env=environment,
        cwd=Path("/"),
    )


def prepared_pane_shell_argv(args: Args, creation_token: str) -> list[str]:
    """Return the manifest-bound, environment-empty shell for a prepared pane."""

    if args.prepared_shell_path is None or args.prepared_env_path is None or not args.prepared_launch_environment:
        raise RuntimeError("prepared launch lost its pinned pane shell/environment binding")
    return [
        str(args.prepared_env_path),
        "-i",
        *(f"{key}={value}" for key, value in args.prepared_launch_environment),
        f"OMO_PREPARED_WINDOW_TOKEN={creation_token}",
        str(args.prepared_shell_path),
        "--noprofile",
        "--norc",
    ]


def current_command(target: str, client_args: Args | None = None) -> str:
    out = tmux_for_args(client_args, ["display-message", "-p", "-t", target, "#{pane_current_command}"])
    return out.stdout.strip() if out.returncode == 0 else ""


def wait_shell(target: str, timeout_s: float = 5.0, client_args: Args | None = None) -> None:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        if current_command(target, client_args) in SHELL_COMMANDS:
            return
        time.sleep(0.25)
    raise RuntimeError(f"tmux target {target} did not return to shell after {timeout_s:g}s.")


def lines_after_launch_marker(lines: list[str], launch_marker: str) -> list[str] | None:
    if not launch_marker:
        return lines
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip() == launch_marker:
            return lines[idx + 1 :]
    return None


def has_codex_update_prompt(lines: list[str]) -> bool:
    text = "\n".join(lines).casefold()
    return all(marker in text for marker in CODEX_UPDATE_PROMPT_MARKERS)


def has_codex_update_success(lines: list[str]) -> bool:
    text = "\n".join(lines).casefold()
    return all(marker in text for marker in CODEX_UPDATE_SUCCESS_MARKERS)


def nonempty_blocks(lines: list[str]) -> list[list[str]]:
    blocks: list[list[str]] = []
    for line in lines:
        if line.strip():
            if not blocks or not blocks[-1]:
                blocks.append([line])
            else:
                blocks[-1].append(line)
        elif blocks and blocks[-1]:
            blocks.append([])
    return [block for block in blocks if block]


def has_codex_trust_prompt(lines: list[str]) -> bool:
    blocks = nonempty_blocks(lines)
    if len(blocks) < 4:
        return False
    confirm, choices, prompt = blocks[-1], blocks[-2], blocks[-3]
    header_idx = -4
    if len(blocks) >= 5 and CODEX_TRUST_NOTE_RE.fullmatch(" ".join(line.strip() for line in blocks[-4])) is not None:
        header_idx = -5
    return (
        len(confirm) == 1
        and CODEX_TRUST_CONFIRM_RE.fullmatch(confirm[0]) is not None
        and len(choices) == 2
        and CODEX_TRUST_YES_RE.fullmatch(choices[0]) is not None
        and CODEX_TRUST_NO_RE.fullmatch(choices[1]) is not None
        and " ".join(line.strip() for line in prompt) == CODEX_TRUST_TEXT
        and len(blocks) >= -header_idx
        and len(blocks[header_idx]) == 1
        and CODEX_TRUST_CWD_RE.fullmatch(blocks[header_idx][0]) is not None
    )


def marker_is_fresh(launch_marker: str, baseline_lines: tuple[str, ...] | None) -> bool:
    return bool(launch_marker and baseline_lines is not None and all(line.strip() != launch_marker for line in baseline_lines))


def exact_pane_id_for_args(target: str, client_args: Args | None = None) -> str:
    if client_args is None or client_args.prepared_runtime_path is None:
        return exact_pane_id(target)
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?", target)
    if match is None:
        return ""
    session, window, pane = match.group(1), match.group(2), match.group(3) or "0"
    canonical = f"{session}:{int(window)}.{int(pane)}"
    result = tmux_for_args(
        client_args,
        ["display-message", "-p", "-t", canonical, "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}"],
    )
    resolved, separator, pane_id = result.stdout.strip().partition("\t") if result.returncode == 0 else ("", "", "")
    return pane_id if separator and resolved == canonical and re.fullmatch(r"%[0-9]+", pane_id) is not None else ""


def require_same_launch_pane(target: str, pane_id: str, client_args: Args | None = None) -> None:
    if exact_pane_id_for_args(target, client_args) != pane_id:
        raise RuntimeError(f"tmux target {target} no longer identifies launched pane {pane_id}.")


def has_live_codex_launch(pane_id: str, client_args: Args | None = None) -> bool:
    try:
        result = tmux_for_args(client_args, ["display-message", "-p", "-t", pane_id, "#{pane_pid}"])
        pane_pid = int(result.stdout.strip()) if result.returncode == 0 else 0
        processes = read_processes()
    except (OSError, RotationError, subprocess.SubprocessError, ValueError):
        return False
    launches = [
        process
        for process in processes.values()
        if process.state != "Z" and process_is_under(process.pid, pane_pid, processes) and is_codex_launch_argv(process.argv)
    ]
    return pane_pid > 1 and len(launches) == 1


def send_launch_enter(target: str, pane_id: str = "", *, require_codex_launch: bool = False, client_args: Args | None = None) -> None:
    if require_codex_launch and (not pane_id or not has_live_codex_launch(pane_id, client_args)):
        raise RuntimeError(f"tmux pane {pane_id or target} does not contain the launched Codex process.")
    delivery_target = target
    if pane_id:
        require_same_launch_pane(target, pane_id, client_args)
        delivery_target = pane_id
    _ = tmux_for_args(client_args, ["send-keys", "-t", delivery_target, "Enter"], check=True)


def wait_codex_update_finished(target: str, launch_marker: str, timeout_s: float = 120.0, pane_id: str = "", client_args: Args | None = None) -> str:
    deadline_s = time.monotonic() + timeout_s
    while time.monotonic() < deadline_s:
        captured = capture_pane(pane_id, 200, require=True, client_args=client_args) if pane_id else tail(target, 200)
        lines = lines_after_launch_marker(captured, launch_marker)
        if lines is not None and has_codex_update_success(lines):
            return CODEX_LAUNCH_UPDATED
        time.sleep(0.25)
    raise RuntimeError(f"Codex update did not finish after {timeout_s:g}s.")


def wait_command_started(
    target: str,
    timeout_s: float = 5.0,
    launch_marker: str = "",
    pane_id: str = "",
    baseline_lines: tuple[str, ...] | None = None,
    client_args: Args | None = None,
) -> str:
    deadline_s = time.monotonic() + timeout_s
    last_command = ""
    last_status = "unknown"
    saw_non_shell = False
    saw_unattributed_trust_prompt = False
    trust_confirmed = False
    trust_allowed = bool(pane_id and not is_human_tmux_session(target) and marker_is_fresh(launch_marker, baseline_lines))
    baseline_has_trust_prompt = baseline_lines is not None and has_codex_trust_prompt(current_block(list(baseline_lines)).lines)
    inspection_target = pane_id or target
    while time.monotonic() < deadline_s:
        captured = capture_pane(pane_id, 200, require=True, client_args=client_args) if pane_id else tail(target, 200)
        lines = lines_after_launch_marker(captured, launch_marker)
        if lines is None:
            block = current_block(captured)
            active_status = status(captured, block)
            active_command = current_command(inspection_target, client_args)
            trust_attributed = trust_allowed and not baseline_has_trust_prompt and active_command in CODEX_LAUNCH_PANE_COMMANDS
            if active_command == "agent":
                return CODEX_LAUNCH_STARTED
            if trust_attributed and active_status == "not_codex" and has_codex_trust_prompt(block.lines):
                last_status = "directory trust confirmation still visible"
                if not trust_confirmed:
                    send_launch_enter(target, pane_id, require_codex_launch=True, client_args=client_args)
                    trust_confirmed = True
            elif active_status == "not_codex" and has_codex_trust_prompt(block.lines):
                last_status = "unattributed directory trust confirmation visible"
                saw_unattributed_trust_prompt = True
            elif trust_confirmed:
                last_status = active_status
                if last_status != "not_codex":
                    return CODEX_LAUNCH_STARTED
            else:
                last_status = "launch marker not visible"
        else:
            if has_codex_update_prompt(lines):
                send_launch_enter(target, pane_id, client_args=client_args)
                return wait_codex_update_finished(target, launch_marker, pane_id=pane_id, client_args=client_args)
            block = current_block(lines)
            active_status = status(lines, block)
            active_command = current_command(inspection_target, client_args)
            trust_attributed = trust_allowed and not baseline_has_trust_prompt and active_command in CODEX_LAUNCH_PANE_COMMANDS
            if active_command == "agent":
                return CODEX_LAUNCH_STARTED
            if trust_attributed and active_status == "not_codex" and has_codex_trust_prompt(block.lines):
                last_status = "directory trust confirmation still visible"
                if not trust_confirmed:
                    send_launch_enter(target, pane_id, require_codex_launch=True, client_args=client_args)
                    trust_confirmed = True
            elif active_status == "not_codex" and has_codex_trust_prompt(block.lines):
                last_status = "unattributed directory trust confirmation visible"
                saw_unattributed_trust_prompt = True
            else:
                last_status = active_status
                if last_status != "not_codex":
                    return CODEX_LAUNCH_STARTED
        last_command = active_command
        if last_command and last_command not in SHELL_COMMANDS:
            saw_non_shell = True
        time.sleep(0.05)
    if trust_confirmed and last_status == "directory trust confirmation still visible":
        raise RuntimeError(f"Codex directory trust confirmation did not advance after {timeout_s:g}s.")
    if saw_unattributed_trust_prompt:
        raise RuntimeError(f"Codex launch not verified after {timeout_s:g}s: {last_status}")
    if saw_non_shell:
        return CODEX_LAUNCH_STARTED
    raise RuntimeError(f"Codex launch not verified after {timeout_s:g}s: pane command={last_command or 'unknown'}, status={last_status}")


def new_window_command(args: Args, create_session: bool = False) -> list[str]:
    name = args.window_name or Path(args.task_file).stem
    if create_session:
        return [
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_id}\t#{session_name}:#{window_index}\t#{pane_id}",
            "-s",
            args.tmux_session.lstrip("="),
            "-n",
            name,
            "-c",
            str(args.workdir),
        ]
    return ["new-window", "-P", "-F", "#{session_id}\t#{session_name}:#{window_index}\t#{pane_id}", "-t", target(args), "-n", name, "-c", str(args.workdir)]


def cleanup_created_session(session_id: str, session_name: str) -> None:
    """Remove only the newly returned session while its exact identity still matches."""

    if TMUX_SESSION_ID_RE.fullmatch(session_id) is None or TMUX_SESSION_RE.fullmatch(session_name) is None:
        return
    condition = f"#{{&&:#{{==:#{{session_id}},{session_id}}},#{{==:#{{session_name}},{session_name}}}}}"
    _ = tmux(["if-shell", "-t", session_id, "-F", condition, f"kill-session -t {shlex.quote(session_id)}", ""])


def launch_input_state(path: Path | None) -> str:
    """Describe one launch input without retaining its content in the error summary."""
    if path is None:
        return "none"
    try:
        if not path.exists():
            return f"path={path!s} exists=false"
        stat = path.stat()
        digest = "omitted"
        if path.is_file() and stat.st_size <= 1_000_000:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"path={path!s} exists=true file={path.is_file()} size={stat.st_size} sha256={digest}"
    except OSError as error:
        return f"path={path!s} state_error={error}"


def retain_tmux_launch_failure(args: Args, command: list[str], error: subprocess.CalledProcessError | subprocess.TimeoutExpired | OSError) -> Path:
    """Persist enough evidence to diagnose a tmux launch failure after the caller exits."""
    try:
        session_state = tmux_for_args(
            args if args.prepared_runtime_path is not None else None,
            ["list-windows", "-t", args.tmux_session, "-F", "#{window_index}:#{window_name}:#{pane_current_command}:#{pane_current_path}:#{pane_id}"],
        )
    except (OSError, subprocess.SubprocessError) as state_error:
        session_state = subprocess.CompletedProcess([], 1, "", str(state_error))
    task = task_path(args.root, args.task_file)
    tmux_env = {key: os.environ.get(key, "") for key in ("TMUX", "TMUX_PANE", "TERM", "OMO_AGENT_TMUX_TARGET", "OMO_MANAGER_TMUX_TARGET")}
    safe_args = replace(
        args,
        codex_flags=tuple("<redacted>" for _ in args.codex_flags),
        human_email_file=None,
        human_email_lines=None,
        human_email_text=None,
    )
    if isinstance(error, subprocess.CalledProcessError):
        exit_status = error.returncode
    elif isinstance(error, subprocess.TimeoutExpired):
        exit_status = f"timeout after {error.timeout}s"
    else:
        exit_status = f"{type(error).__name__}: {error}"
    lines = [
        f"omo_task tmux {command[0]} failure",
        f"args: {safe_args!r}",
        f"process_cwd: {Path.cwd()}",
        f"tmux_env: {tmux_env!r}",
        f"tmux_command: {shlex.join(['tmux', *command])}",
        f"exit_status: {exit_status}",
        f"stdout: {getattr(error, 'stdout', '') or ''}",
        f"stderr: {getattr(error, 'stderr', '') or ''}",
        f"task: {launch_input_state(task)}",
        f"prompt: {launch_input_state(args.prompt_file)}",
        f"human_email: {'present (redacted)' if args.human_email_file is not None else 'none'}",
        f"prelaunch_source: {launch_input_state(args.prelaunch_source)}",
        f"workdir: {launch_input_state(args.workdir)}",
        f"effective_window_name: {args.window_name or Path(args.task_file).stem}",
        f"session_windows_exit_status: {session_state.returncode}",
        f"session_windows_stdout: {session_state.stdout}",
        f"session_windows_stderr: {session_state.stderr}",
    ]
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="omo-task-tmux-launch-failure-",
        suffix=".log",
        delete=False,
    ) as evidence:
        _ = evidence.write("\n".join(lines) + "\n")
    return Path(evidence.name)


def start_codex(target: str, args: Args) -> None:
    vl_agent = is_vl_agent(args.task_file, target)
    if vl_agent and args.prompt_file is None and not args.resume_idle:
        raise ValueError("VL launches require --prompt-file so the end-goal and reviewer guidance has task-local context.")
    prepared_exact_prompt = args.prepared_runtime_path is not None
    manager_file = args.root / "MANAGER.md" if args.is_manager else None
    excerpt = human_email_excerpt(args)
    human_instruction_file = write_human_instruction_file(excerpt, human_email_source(args)) if excerpt else None
    manager_source = args.manager_target.strip() or current_manager_target() or "unknown"
    manager_delegation_file = (
        args.prompt_file
        if prepared_exact_prompt
        else write_manager_delegation_file(args.prompt_file, manager_source)
        if args.prompt_file is not None and args.prompt_file.is_file()
        else args.prompt_file
    )
    remove_manager_delegation_file = not prepared_exact_prompt and manager_delegation_file is not None and manager_delegation_file != args.prompt_file
    try:
        pane_id = exact_pane_id_for_args(target, args if prepared_exact_prompt else None)
        if not pane_id:
            raise RuntimeError(f"new task target {target} does not resolve to its exact pane.")
        command = codex_cmd(
            args.session_id,
            args.reasoning_effort,
            args.codex_flags,
            None if prepared_exact_prompt else manager_delegation_file,
            effective_tool(args),
            vl_agent,
            args.model,
            manager_file,
            human_instruction_file,
            not args.resume_idle and not prepared_exact_prompt,
            args.workdir,
            args.prepared_runtime_path,
        )
        if prepared_exact_prompt:
            if manager_delegation_file is None:
                raise RuntimeError("prepared launch lost its descriptor-captured exact prompt")
            # The prepared shell is pinned Bash.  Its file-read substitution is
            # a builtin, so launching does not acquire an extra, unbound `cat`
            # executable from even the deliberately minimal PATH.
            command = f'{command} "$(< {shlex.quote(str(manager_delegation_file))})"'
        for attempt in range(2):
            baseline_lines = tuple(capture_pane(pane_id, 200, require=True, client_args=args if prepared_exact_prompt else None))
            launch_marker = new_launch_marker()
            if not marker_is_fresh(launch_marker, baseline_lines):
                raise RuntimeError("new Codex launch marker was already present in the pane baseline.")
            if prepared_exact_prompt:
                shell_launch = prepared_shell_launch_command(command, target, args, launch_marker)
            else:
                shell_launch = shell_cmd(
                    worker_command(
                        command,
                        target,
                        args.prelaunch_source,
                        launch_marker,
                        args.amh_caller_agent,
                    )
                )
            require_same_launch_pane(target, pane_id, args if prepared_exact_prompt else None)
            _ = tmux_for_args(args if prepared_exact_prompt else None, ["send-keys", "-t", pane_id, shell_launch, "Enter"], check=True)
            if wait_command_started(
                target,
                launch_marker=launch_marker,
                pane_id=pane_id,
                baseline_lines=baseline_lines,
                client_args=args if prepared_exact_prompt else None,
            ) != CODEX_LAUNCH_UPDATED:
                return
            if prepared_exact_prompt:
                wait_shell(pane_id, timeout_s=15.0, client_args=args)
            else:
                wait_shell(pane_id, timeout_s=15.0)
        raise RuntimeError("Codex update completed but relaunch showed the update prompt again.")
    finally:
        if human_instruction_file is not None:
            human_instruction_file.unlink(missing_ok=True)
        if remove_manager_delegation_file and manager_delegation_file is not None:
            manager_delegation_file.unlink(missing_ok=True)


def prepared_tmux_pane_inventory(args: Args) -> dict[str, TmuxPaneCreationIdentity]:
    result = tmux_for_args(
        args,
        [
            "list-panes",
            "-a",
            "-F",
            "#{session_id}\t#{window_id}\t#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_pid}\t#{pane_dead}",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("prepared launch could not authenticate the pre/post-create tmux pane inventory")
    inventory: dict[str, TmuxPaneCreationIdentity] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 6
            or TMUX_SESSION_ID_RE.fullmatch(fields[0]) is None
            or re.fullmatch(r"@[0-9]+", fields[1]) is None
            or re.fullmatch(r"%[0-9]+", fields[2]) is None
            or TMUX_TARGET_RE.fullmatch(fields[3]) is None
            or not fields[4].isdigit()
            or fields[5] not in {"0", "1"}
            or fields[2] in inventory
        ):
            raise RuntimeError("prepared launch received a malformed or duplicate tmux pane inventory")
        if fields[5] == "1":
            continue
        pane_pid = int(fields[4])
        start_ticks = process_start_ticks(pane_pid)
        if pane_pid <= 1 or start_ticks is None:
            raise RuntimeError("prepared launch could not bind one live process identity to each tmux pane")
        inventory[fields[2]] = TmuxPaneCreationIdentity(
            fields[0],
            fields[1],
            fields[2],
            canonical_tmux_pane_text(fields[3]),
            pane_pid,
            start_ticks,
        )
    return inventory


def pane_has_creation_token(identity: TmuxPaneCreationIdentity, token: str) -> bool:
    try:
        environment = Path(f"/proc/{identity.pane_pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    marker = f"OMO_PREPARED_WINDOW_TOKEN={token}".encode()
    return marker in environment and process_start_ticks(identity.pane_pid) == identity.process_start_ticks


def cleanup_only_proven_new_pane(
    before: dict[str, TmuxPaneCreationIdentity],
    requested_target: str,
    creation_token: str,
    args: Args,
    *,
    absence_is_success: bool = False,
) -> None:
    after = prepared_tmux_pane_inventory(args)
    new = [identity for pane_id, identity in after.items() if pane_id not in before]
    if not new and absence_is_success:
        return
    canonical_requested = canonical_tmux_pane_text(requested_target)
    matching = [identity for identity in new if identity.target == canonical_requested and pane_has_creation_token(identity, creation_token)]
    if len(new) != 1 or len(matching) != 1:
        raise RuntimeError("prepared launch could not prove one newly created transaction-owned pane; no existing pane was killed")
    identity = matching[0]
    cleanup_prepared_launch_window(
        LaunchWindow(identity.target, identity.pane_id, identity.session_id, window_id=identity.window_id),
        args,
    )


def new_window_bound(args: Args) -> LaunchWindow:
    if args.workdir is None:
        tmux_target = target(args)
        pane_id = exact_pane_id(tmux_target)
        return LaunchWindow(tmux_target, pane_id, "")
    session = launch_session(args)
    bound_args = replace(args, tmux_session=session.target)
    if session.create:
        bound_args = replace(args, tmux_session=session.name)
    command = new_window_command(bound_args, session.create)
    creation_token = uuid.uuid4().hex if args.prepared_runtime_path is not None else ""
    if creation_token:
        command.extend(("-e", f"OMO_PREPARED_WINDOW_TOKEN={creation_token}"))
        command.extend(prepared_pane_shell_argv(args, creation_token))
    requested_target = (
        canonical_tmux_pane_text(f"{session.name}:{'0' if session.create else args.tmux_window}.0")
        if args.prepared_runtime_path is not None or session.create
        else ""
    )
    panes_before = prepared_tmux_pane_inventory(args) if args.prepared_runtime_path is not None else {}
    try:
        out = tmux_for_args(args if args.prepared_runtime_path is not None else None, command, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        reconcile_error: Exception | None = None
        if args.prepared_runtime_path is not None:
            try:
                cleanup_only_proven_new_pane(
                    panes_before,
                    requested_target,
                    creation_token,
                    args,
                    absence_is_success=True,
                )
            except Exception as exc:
                reconcile_error = exc
        try:
            evidence = retain_tmux_launch_failure(bound_args, command, error)
        except OSError as evidence_error:
            raise RuntimeError(f"tmux {command[0]} failed; diagnostic retention failed: {evidence_error}") from error
        if reconcile_error is not None:
            raise RuntimeError(
                f"tmux {command[0]} failed and post-failure creation reconciliation failed closed: {reconcile_error}; diagnostic: {evidence}"
            ) from error
        raise RuntimeError(f"tmux {command[0]} failed; diagnostic: {evidence}") from error
    fields = out.stdout.rstrip("\r\n").split("\t")
    returned_session_id = fields[0] if fields else ""
    if len(fields) != 3:
        if session.create:
            cleanup_created_session(returned_session_id, session.name)
        elif args.prepared_runtime_path is not None:
            cleanup_only_proven_new_pane(panes_before, requested_target, creation_token, args)
        raise RuntimeError("tmux new-window did not return bound session, target, and pane identity.")
    created_session_id, tmux_target, pane_id = fields
    expected_session_id = created_session_id if session.create else session.target
    if (
        TMUX_SESSION_ID_RE.fullmatch(created_session_id) is None
        or created_session_id != expected_session_id
        or TMUX_TARGET_RE.fullmatch(tmux_target) is None
        or (requested_target and canonical_tmux_pane_text(tmux_target) != requested_target)
        or re.fullmatch(r"%[0-9]+", pane_id) is None
    ):
        if session.create:
            cleanup_created_session(created_session_id, session.name)
        elif args.prepared_runtime_path is not None:
            cleanup_only_proven_new_pane(panes_before, requested_target, creation_token, args)
        raise RuntimeError("tmux launch identity did not match the requested session and pane.")
    window = LaunchWindow(tmux_target, pane_id, created_session_id, session.create, session.name)
    if args.prepared_runtime_path is not None:
        created = prepared_tmux_pane_inventory(args)
        identity = created.get(pane_id)
        if (
            pane_id in panes_before
            or identity is None
            or identity.session_id != created_session_id
            or identity.target != canonical_tmux_pane_text(tmux_target)
            or not pane_has_creation_token(identity, creation_token)
        ):
            cleanup_only_proven_new_pane(panes_before, requested_target, creation_token, args)
            raise RuntimeError("tmux new-window identity was not one newly created transaction-owned pane.")
        window = replace(window, window_id=identity.window_id)
    try:
        if args.prepared_runtime_path is not None:
            wait_shell(pane_id, client_args=args)
        else:
            wait_shell(pane_id)
    except Exception as exc:
        if window.created_session:
            cleanup_created_session(window.session_id, window.session_name)
            raise
        try:
            cleanup_prepared_launch_window(window, args)
        except Exception as cleanup_error:
            raise RuntimeError(f"created-window failure could not prove positive pane/window absence: {cleanup_error}") from exc
        raise
    return window


def verify_launch_window(window: LaunchWindow, client_args: Args | None = None) -> None:
    """Fail if the created pane's symbolic target or session identity rebound."""

    result = tmux_for_args(client_args, ["display-message", "-p", "-t", f"={window.target}", "#{session_id}\t#{pane_id}"])
    if result.returncode != 0 or result.stdout.strip() != f"{window.session_id}\t{window.pane_id}":
        if window.created_session:
            cleanup_created_session(window.session_id, window.session_name)
        raise RuntimeError(f"new task target {window.target} changed before task registration.")


def new_window(args: Args) -> str:
    window = new_window_bound(args)
    return LaunchTarget(window.target, window.pane_id, window.session_id, window.created_session, window.session_name)


def ensure_task_file(args: Args, tmux_target: str) -> Path:
    path = task_path(args.root, args.task_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    before = path.stat() if existed else None
    stored_existing = path.read_text(encoding="utf-8") if existed else ""
    existing = stored_existing
    metadata = parse_task_metadata(existing, args.root) if existed else None
    if metadata is not None and metadata.version != TASK_FRONTMATTER_V2 and v2_enabled(args.root):
        raise ValueError("v1 task writes are disabled after v2 enablement.")
    if not existed:
        text = new_task_text(args, tmux_target)
        validate_runat_goal_tree(text)
    elif metadata is not None:
        text = launched_frontmatter_text(existing, args, tmux_target) if args.workdir is not None else existing
    else:
        text = upsert_header(existing, header(tmux_target, effective_tool(args))) if args.workdir is not None else existing
    if existed and args.manager_target and metadata is not None:
        text = replace_frontmatter_fields(text, {"managerat": args.manager_target})
    elif existed and args.manager_target:
        text = upsert_managerat(text, args.manager_target)
    if args.prompt_file is not None:
        if existed:
            sep = "" if not text or text.endswith("\n") else "\n"
            manager_source = metadata.managerat if metadata is not None else managerat_for_task(args, tmux_target)
            text += sep + task_instruction_text(args, manager_source) + "\n"
    if text != stored_existing or not existed:
        if existed:
            if parse_task_metadata(text, args.root) is None:
                validate_runat_header(text)
                validate_managerat_metadata(text)
        metadata = parse_task_metadata(text, args.root)
        if metadata is not None and metadata.version == TASK_FRONTMATTER_V2 and not v2_enabled(args.root):
            raise ValueError("v2 task mutation is disabled until migration validation and watcher enablement are complete.")
        if before is not None:
            atomic_replace_if_unchanged(path, text, before)
        else:
            with task_file_lock(path):
                if path.exists():
                    raise ValueError("task file appeared while launch metadata was being prepared; retry")
                _ = path.write_text(text, encoding="utf-8")
    return path


def todo_line(args: Args, tmux_target: str) -> str:
    parts = [task_ref(args.root, args.task_file)]
    if tmux_target:
        parts.append(tmux_target)
    return " ".join(parts)


def refreshed_todo_entry(existing: str, ref: str, tmux_target: str) -> str:
    leading = existing[: len(existing) - len(existing.lstrip())]
    stripped = existing.strip()
    token, _sep, rest = stripped.partition(" ")
    rest = rest.lstrip()
    if not tmux_target:
        return f"{leading}{ref}" if not rest else f"{leading}{ref} {rest}"
    if not rest:
        return f"{leading}{ref} {tmux_target}"
    target_match = re.match(r"(?P<target>[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)(?P<tail>.*)$", rest)
    if target_match is not None:
        return f"{leading}{ref} {tmux_target}{target_match.group('tail')}"
    loose_target_match = re.match(r"(?P<session>[A-Za-z][A-Za-z0-9_-]*)\s+(?P<window>\d+)(?P<tail>.*)$", rest)
    if loose_target_match is not None:
        return f"{leading}{ref} {tmux_target}{loose_target_match.group('tail')}"
    return f"{leading}{ref} {tmux_target} {rest}"


def link_todo(args: Args, tmux_target: str, *, locked: bool = False) -> None:
    todo = args.root / "TODO.md"
    lock = contextlib.nullcontext() if locked else task_file_lock(todo)
    with lock:
        line = todo_line(args, tmux_target)
        lines = todo.read_text(encoding="utf-8").splitlines() if todo.exists() else ["current:", ""]
        ref = task_ref(args.root, args.task_file)
        aliases = {args.task_file, ref, str(task_path(args.root, args.task_file))}
        for idx, existing in enumerate(lines):
            stripped = existing.strip()
            if not stripped:
                continue
            token = stripped.split(maxsplit=1)[0]
            if token not in aliases:
                continue
            updated = refreshed_todo_entry(existing, ref, tmux_target)
            if existing != updated:
                lines[idx] = updated
                _ = todo.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return
        try:
            current_idx = next(idx for idx, existing in enumerate(lines) if existing.strip() == "current:")
        except StopIteration:
            lines.extend(["", "current:", ""])
            current_idx = len(lines) - 2
        insert_at = current_idx + 1
        while insert_at < len(lines) and not lines[insert_at].strip():
            insert_at += 1
        lines.insert(insert_at, line)
        _ = todo.write_text("\n".join(lines) + "\n", encoding="utf-8")


def dry_run(args: Args) -> None:
    session = launch_session(args) if args.workdir is not None else None
    tmux_target = target(args) if session is None else f"{session.name}:{args.tmux_window or 'DRYRUN'}"
    path = task_path(args.root, args.task_file)
    print(f"task_file: {path}")
    if not args.no_link:
        print(f"todo_line: {todo_line(args, tmux_target)}")
    if session is not None:
        bound_args = replace(args, tmux_session=session.name if session.create else session.target)
        if args.prelaunch_source is not None:
            print(f"prelaunch_source: {args.prelaunch_source}")
        command = ["tmux", *new_window_command(bound_args, session.create)]
        print("tmux: " + " ".join(shlex.quote(part) for part in command))
        launch_target = tmux_target
        manager_file = args.root / "MANAGER.md" if args.is_manager else None
        human_instruction_file = Path(tempfile.gettempdir()) / "omo-human-instruction.DRYRUN" if args.human_email_file is not None else None
        launch_command = codex_cmd(
            args.session_id,
            args.reasoning_effort,
            args.codex_flags,
            args.prompt_file,
            effective_tool(args),
            is_vl_agent(args.task_file, launch_target),
            args.model,
            manager_file,
            human_instruction_file,
            not args.resume_idle,
            args.workdir,
        )
        launch = [
            "tmux",
            "send-keys",
            "-t",
            launch_target,
            shell_cmd(
                worker_command(
                    launch_command,
                    launch_target,
                    args.prelaunch_source,
                    CODEX_LAUNCH_MARKER_DRY_RUN,
                    args.amh_caller_agent,
                )
            ),
            "Enter",
        ]
        print("tmux: " + " ".join(shlex.quote(part) for part in launch))


def capture_pane(pane_id: str, n_lines: int = 80, *, require: bool = False, client_args: Args | None = None) -> list[str]:
    """Capture one already-resolved pane without resolving its target again."""

    command = ["capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{n_lines}"]
    if client_args is None:
        out = subprocess.run(["tmux", *command], capture_output=True, text=True, timeout=5, check=False)
    else:
        out = tmux_for_args(client_args, command)
    if out.returncode != 0 and require:
        raise RuntimeError(f"failed to capture launched tmux pane {pane_id}: {out.stderr.strip()}")
    if out.returncode != 0:
        return []
    lines = [line.rstrip() for line in (out.stdout or "").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def validate_existing_target_runtime(args: Args) -> str:
    """Require registration-only mode to name an exact live managed pane."""

    if args.workdir is not None:
        return ""
    tmux_target = target(args)
    if TMUX_TARGET_RE.fullmatch(tmux_target) is None:
        raise ValueError("runat tmux target must be a full tmux target like `SESSION:WINDOW`.")
    if args.dry_run:
        return ""
    pane_id = exact_pane_id(tmux_target)
    if not pane_id:
        raise ValueError(f"existing-target mode does not create or launch `{tmux_target}`; use --workdir to launch a new worker.")
    lines = capture_pane(pane_id)
    if exact_pane_id(tmux_target) != pane_id:
        raise ValueError(f"existing target `{tmux_target}` changed while it was being inspected; retry or use --workdir to launch a new worker.")
    if effective_tool(args) == "cursor":
        if current_command(pane_id) == "agent":
            return pane_id
        raise ValueError(f"existing-target mode requires a live Cursor Agent process at `{tmux_target}` for `--tool cursor`; use `--tool codex` for a Codex pane or `--workdir` to launch a new worker.")
    target_status = status(lines, current_block(lines))
    if target_status not in {"ready", "running"}:
        raise ValueError(f"existing-target mode requires a ready or running managed agent pane at `{tmux_target}`, got {target_status}; use --workdir to launch a new worker.")
    return pane_id


def validate_inputs(args: Args) -> str:
    if not args.tmux_session:
        raise ValueError("--tmux-session is required.")
    if TMUX_SESSION_RE.fullmatch(args.tmux_session) is None:
        raise ValueError("--tmux-session must be an exact session name starting with a letter and containing only letters, numbers, `_`, or `-`.")
    if args.workdir is not None and not args.workdir.is_dir():
        raise ValueError(f"--workdir must be an existing directory: {args.workdir}")
    if args.workdir is not None and not args.resume_idle and (not args.model.strip() or not args.reasoning_effort.strip()):
        raise ValueError("--workdir requires nonempty --model MODEL and --reasoning-effort EFFORT.")
    if args.resume_idle and not args.session_id:
        raise ValueError("--resume-idle requires --session-id.")
    if args.resume_idle and args.workdir is None:
        raise ValueError("--resume-idle requires --workdir.")
    if args.resume_idle and args.prompt_file is not None:
        raise ValueError("--resume-idle does not accept --prompt-file.")
    if invalid_model := model_error(args.model):
        raise ValueError(invalid_model)
    if args.amh_caller_agent:
        if AMH_AGENT_ID_RE.fullmatch(args.amh_caller_agent) is None:
            raise ValueError("--amh-caller-agent must be a nonempty ASCII AMH agent id using only letters, numbers, `.`, `_`, or `-`.")
        if args.workdir is None:
            raise ValueError("--amh-caller-agent is only valid for a launched worker with --workdir.")
    if (args.human_email_file is None) != (args.human_email_lines is None):
        raise ValueError("--human-email-file and --human-email-lines must be supplied together.")
    if args.human_email_file is not None and args.workdir is None:
        raise ValueError("--human-email-file and --human-email-lines require --workdir.")
    if args.human_email_file is not None:
        _ = human_email_excerpt(args)
    if args.workdir is not None:
        _ = validate_launch_session(args)
    if args.workdir is not None and not args.resume_idle:
        readable_file(DEFAULT_WORKER_INSTRUCTIONS, "worker defaults")
        if is_vl_agent(args.task_file, target(args)):
            readable_file(VL_WORKER_INSTRUCTIONS, "VL worker defaults")
    if args.workdir is not None and args.is_manager and not args.resume_idle:
        readable_file(args.root / "MANAGER.md", "manager instructions")
    if args.prompt_file is not None and not args.prompt_file.is_file():
        raise ValueError(f"prompt file not found: {args.prompt_file}")
    if args.prompt_file is not None:
        _ = manager_delegation(args.prompt_file.read_text(encoding="utf-8"), args.manager_target or current_manager_target() or "unknown")
    if args.prelaunch_source is not None:
        if not args.prelaunch_source.is_file():
            raise ValueError(f"prelaunch source file not found: {args.prelaunch_source}")
        if not os.access(args.prelaunch_source, os.R_OK):
            raise ValueError(f"prelaunch source file is not readable: {args.prelaunch_source}")
    if any(not flag or "\0" in flag or "\n" in flag for flag in args.codex_flags):
        raise ValueError("codex flags must be non-empty single-line argv tokens.")
    raw_model_flag_error = codex_flags_model_error(args.codex_flags)
    if raw_model_flag_error:
        raise ValueError(raw_model_flag_error)
    if args.tool != "pcodx" and any("mcp_servers." in flag for flag in args.codex_flags):
        raise ValueError("MCP server config requires --tool pcodx.")
    if args.tool == "cursor" and args.codex_flags:
        raise ValueError("--codex-flag is only valid for Codex tools.")
    if args.workdir is not None and args.prompt_file is None and is_vl_agent(args.task_file, target(args)) and not args.resume_idle:
        raise ValueError("VL launches require --prompt-file so the end-goal and reviewer guidance has task-local context.")
    if args.workdir is not None and is_vl_agent(args.task_file, target(args)) and not is_vl_submanager_task_file(args.task_file) and not args.manager_target:
        raise ValueError("VL worker launches require --manager-target for the owning submanager.")
    path = task_path(args.root, args.task_file)
    if path.exists() and args.manager_target:
        existing_text = path.read_text(encoding="utf-8")
        metadata = parse_task_metadata(existing_text, args.root)
        if metadata is not None:
            existing_manager_target = metadata.managerat
        else:
            validate_runat_header(existing_text)
            validate_managerat_metadata(existing_text)
            existing_manager_target = managerat_value(existing_text)
        if existing_manager_target and existing_manager_target != args.manager_target:
            raise ValueError(f"existing managerat {existing_manager_target} does not match --manager-target {args.manager_target}.")
    elif path.exists():
        existing_text = path.read_text(encoding="utf-8")
        if parse_task_metadata(existing_text, args.root) is None:
            validate_runat_header(existing_text)
            validate_managerat_metadata(existing_text)
    if path.exists():
        return human_email_excerpt(args)
    tmux_target = "target" if args.workdir is not None else target(args)
    text = new_task_text(args, tmux_target, validate_target=args.workdir is None)
    validate_runat_goal_tree(text)
    return human_email_excerpt(args)


def prepared_launch_receipt_path(journal: Path) -> Path:
    return journal.with_name(f".{journal.name}.launch")


def prepared_launch_receipt_text(
    args: Args,
    *,
    state: str,
    target: str,
    pane_id: str = "",
    pane_pid: int = 0,
    session_id: str = "",
    window_id: str = "",
    process_pid: int = 0,
    process_argv_sha256: str = "",
    protected_inventory_sha256: str = "",
    error: str = "",
) -> bytes:
    fields = (
        "version: v1.0.0",
        "operation: prepared-worker-successor-launch",
        f"state: {state}",
        f"journal: {args.prepared_successor_journal}",
        f"journal-sha256: {args.expected_prepared_journal_sha256}",
        f"task-file: {args.task_file}",
        f"task-sha256: {args.expected_prepared_task_sha256}",
        f"prompt-sha256: {args.expected_prepared_prompt_sha256}",
        f"queue-sha256: {args.expected_prepared_queue_sha256}",
        f"launch-manifest-sha256: {args.expected_prepared_launch_manifest_sha256}",
        f"target: {target}",
        f"pane-id: {pane_id}",
        f"pane-pid: {pane_pid}",
        f"session-id: {session_id}",
        f"window-id: {window_id}",
        f"process-pid: {process_pid}",
        f"process-argv-sha256: {process_argv_sha256}",
        f"protected-inventory-sha256: {protected_inventory_sha256}",
        f"error-sha256: {hashlib.sha256(error.encode()).hexdigest() if error else ''}",
        "",
    )
    return "\n".join(fields).encode()


def prepared_launch_receipt_fields(data: bytes) -> dict[str, str]:
    try:
        lines = data.decode().splitlines()
    except UnicodeDecodeError as exc:
        raise RuntimeError("prepared launch receipt is not UTF-8") from exc
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(": ")
        if not separator or not key or key in fields:
            raise RuntimeError("prepared launch receipt has malformed or duplicate fields")
        fields[key] = value
    required = {
        "version",
        "operation",
        "state",
        "journal",
        "journal-sha256",
        "task-file",
        "task-sha256",
        "prompt-sha256",
        "queue-sha256",
        "launch-manifest-sha256",
        "target",
        "pane-id",
        "pane-pid",
        "session-id",
        "window-id",
        "process-pid",
        "process-argv-sha256",
        "protected-inventory-sha256",
        "error-sha256",
    }
    if set(fields) != required or fields["version"] != "v1.0.0" or fields["operation"] != "prepared-worker-successor-launch":
        raise RuntimeError("prepared launch receipt schema is invalid")
    return fields


def parsed_prepared_launch_receipt(data: bytes) -> PreparedLaunchReceipt:
    fields = prepared_launch_receipt_fields(data)
    try:
        pane_pid = int(fields["pane-pid"])
        process_pid = int(fields["process-pid"])
    except ValueError as exc:
        raise RuntimeError("prepared launch receipt has non-integer process identity fields") from exc
    if (
        fields["state"] not in {"prepared", "window", "started", "published", "committed", "failed"}
        or TMUX_TARGET_RE.fullmatch(fields["target"]) is None
        or (fields["pane-id"] and re.fullmatch(r"%[0-9]+", fields["pane-id"]) is None)
        or (fields["session-id"] and TMUX_SESSION_ID_RE.fullmatch(fields["session-id"]) is None)
        or (fields["window-id"] and re.fullmatch(r"@[0-9]+", fields["window-id"]) is None)
        or pane_pid < 0
        or process_pid < 0
        or (fields["process-argv-sha256"] and re.fullmatch(r"[0-9a-f]{64}", fields["process-argv-sha256"]) is None)
        or (fields["protected-inventory-sha256"] and re.fullmatch(r"[0-9a-f]{64}", fields["protected-inventory-sha256"]) is None)
        or (fields["error-sha256"] and re.fullmatch(r"[0-9a-f]{64}", fields["error-sha256"]) is None)
    ):
        raise RuntimeError("prepared launch receipt contains invalid lifecycle or identity fields")
    return PreparedLaunchReceipt(
        fields["state"],
        fields["target"],
        fields["pane-id"],
        pane_pid,
        fields["session-id"],
        fields["window-id"],
        process_pid,
        fields["process-argv-sha256"],
        fields["protected-inventory-sha256"],
    )


def prepared_protected_inventory_sha256(inventory: dict[str, object]) -> str:
    values: dict[str, object] = {}
    for target_name, identity in sorted(inventory.items()):
        if identity is None:
            values[target_name] = None
        else:
            values[target_name] = {
                "target": getattr(identity, "target", ""),
                "pane_id": getattr(identity, "pane_id", ""),
                "pane_pid": getattr(identity, "pid", getattr(identity, "pane_pid", 0)),
                "process_start_ticks": getattr(identity, "start_ticks", getattr(identity, "process_start_ticks", 0)),
            }
    import json

    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_prepared_receipt_binding(
    data: bytes,
    args: Args,
    binding,
    protected_inventory_sha256: str,
) -> PreparedLaunchReceipt:
    fields = prepared_launch_receipt_fields(data)
    expected = {
        "journal": str(args.prepared_successor_journal),
        "journal-sha256": args.expected_prepared_journal_sha256,
        "task-file": args.task_file,
        "task-sha256": args.expected_prepared_task_sha256,
        "prompt-sha256": args.expected_prepared_prompt_sha256,
        "queue-sha256": args.expected_prepared_queue_sha256,
        "launch-manifest-sha256": args.expected_prepared_launch_manifest_sha256,
        "target": binding.target,
        "protected-inventory-sha256": protected_inventory_sha256,
    }
    if any(fields[key] != value for key, value in expected.items()):
        raise RuntimeError("prepared launch receipt binding or protected-pane inventory changed")
    receipt = parsed_prepared_launch_receipt(data)
    empty_identity = not any((receipt.pane_id, receipt.pane_pid, receipt.session_id, receipt.window_id, receipt.process_pid, receipt.process_argv_sha256))
    if receipt.state == "prepared" and not empty_identity:
        raise RuntimeError("prepared launch receipt exposes an identity before window creation")
    if receipt.state in {"window", "started", "published", "committed"} and (
        not receipt.pane_id or receipt.pane_pid <= 1 or not receipt.session_id or not receipt.window_id
    ):
        raise RuntimeError("prepared launch receipt lacks its complete tmux creation identity")
    if receipt.state in {"started", "published", "committed"} and (
        receipt.process_pid <= 1 or not receipt.process_argv_sha256
    ):
        raise RuntimeError("prepared launch receipt lacks its exact Cursor process identity")
    if receipt.state != "failed" and fields["error-sha256"]:
        raise RuntimeError("non-failed prepared launch receipt contains an error identity")
    return receipt


def require_prepared_launch_sole_owner(binding) -> None:
    from omo_manager.omo_task_status import authoritative_active_target_task_paths
    from omo_manager.omo_worker_successor import active_owners

    expected = (binding.successor_path.resolve(),)
    problems: list[str] = []
    try:
        authoritative = authoritative_active_target_task_paths(binding.root, binding.target)
        if authoritative != expected:
            problems.append("authoritative resolver no longer reports exactly the prepared successor")
    except Exception as exc:
        problems.append(f"authoritative resolver failed: {exc}")
    try:
        raw = active_owners(binding.root, binding.target, {})
        if raw != expected:
            problems.append("raw active-owner scan no longer reports exactly the prepared successor")
    except Exception as exc:
        problems.append(f"raw active-owner scan failed: {exc}")
    if problems:
        raise RuntimeError("prepared successor lost dual sole-ownership proof: " + "; ".join(problems))


def require_prepared_protected_inventory(binding, expected_sha256: str, args: Args) -> None:
    inventory = prepared_tmux_pane_inventory(args)
    current = {
        target_name: next((identity for identity in inventory.values() if identity.target == target_name), None)
        for target_name in binding.protected_targets
    }
    if prepared_protected_inventory_sha256(current) != expected_sha256:
        raise RuntimeError("prepared successor protected-pane inventory changed")


def authenticated_prepared_launch_window(receipt: PreparedLaunchReceipt, args: Args) -> LaunchWindow:
    inventory = prepared_tmux_pane_inventory(args)
    identity = inventory.get(receipt.pane_id)
    if (
        identity is None
        or identity.target != canonical_tmux_pane_text(receipt.target)
        or identity.session_id != receipt.session_id
        or identity.window_id != receipt.window_id
    ):
        raise RuntimeError("prepared launch recovery could not authenticate its exact transaction-created tmux identity")
    _pane_id, pane_pid, _command = prepared_launch_pane_identity(receipt.target, args, receipt.pane_id)
    if pane_pid != receipt.pane_pid:
        raise RuntimeError("prepared launch recovery pane process identity changed")
    return LaunchWindow(receipt.target, receipt.pane_id, receipt.session_id, window_id=receipt.window_id)


def maybe_crash_prepared_launch(phase: str) -> None:
    """Test seam for a process death after one durable launch boundary."""

    if os.environ.get("OMO_PREPARED_LAUNCH_CRASH_AFTER") == phase:
        os._exit(87)


def prepared_launch_pane_identity(target: str, args: Args, expected_pane_id: str = "") -> tuple[str, int, str]:
    result = tmux_for_args(
        args,
        [
            "display-message",
            "-p",
            "-t",
            f"={target}",
            "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}",
        ]
    )
    fields = result.stdout.rstrip("\r\n").split("\t") if result.returncode == 0 else []
    if (
        len(fields) != 4
        or canonical_tmux_pane(fields[0]) != canonical_tmux_pane(target)
        or re.fullmatch(r"%[0-9]+", fields[1]) is None
        or not fields[2].isdigit()
    ):
        raise RuntimeError("prepared successor launch could not prove the target pane/process identity.")
    if expected_pane_id and fields[1] != expected_pane_id:
        raise RuntimeError("prepared successor launch pane identity changed.")
    return fields[1], int(fields[2]), fields[3]


def prepared_launch_metadata(text: str, root: Path):
    result = parse_task_metadata(text, root)
    if result is None:
        raise RuntimeError("prepared successor task has no frontmatter.")
    return result


def prepared_cursor_process_proof(
    pane_id: str,
    args: Args,
    runtime: dict[str, object],
    exact_prompt: bytes,
    *,
    proc_root: Path = Path("/proc"),
) -> CursorProcessProof:
    """Authenticate one installed Cursor runtime with its exact launch argv."""

    from omo_manager.omo_worker_successor import read_frozen_prompt

    pane = tmux_for_args(args, ["display-message", "-p", "-t", pane_id, "#{pane_pid}"])
    try:
        pane_pid = int(pane.stdout.strip()) if pane.returncode == 0 else 0
        prompt = exact_prompt.decode()
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("prepared Cursor launch has no usable pane PID or UTF-8 prompt") from exc
    if pane_pid <= 1 or args.workdir is None:
        raise RuntimeError("prepared Cursor launch has no usable pane PID or workdir")
    launcher = Path(str(runtime["launcher_resolved"]))
    node = Path(str(runtime["node_path"]))
    index = Path(str(runtime["index_path"]))
    for path, expected in (
        (launcher, str(runtime["launcher_sha256"])),
        (node, str(runtime["node_sha256"])),
        (index, str(runtime["index_sha256"])),
    ):
        if hashlib.sha256(read_frozen_prompt(path).data).hexdigest() != expected:
            raise RuntimeError("prepared Cursor installed runtime bytes changed")
    expected_tail = (
        str(index),
        "--force",
        "--sandbox",
        "disabled",
        "--trust",
        "--workspace",
        str(args.workdir),
        "--model",
        f"{args.model}-{args.reasoning_effort}" if args.reasoning_effort else args.model,
        prompt,
    )
    try:
        processes = read_processes(proc_root)
    except (OSError, RotationError) as exc:
        raise RuntimeError(f"prepared Cursor process inventory failed: {exc}") from exc
    matches: list[CursorProcessProof] = []
    for process in processes.values():
        if process.state == "Z" or not process_is_under(process.pid, pane_pid, processes) or not process.argv:
            continue
        argv = process.argv
        tail_index = 2 if len(argv) > 2 and argv[1] == "--use-system-ca" else 1
        if argv[0] != str(launcher) or tuple(argv[tail_index:]) != expected_tail:
            continue
        try:
            executable = (proc_root / str(process.pid) / "exe").resolve(strict=True)
        except OSError:
            continue
        if executable != node:
            continue
        try:
            environment_data = (proc_root / str(process.pid) / "environ").read_bytes()
            environment_parts = environment_data.rstrip(b"\0").split(b"\0") if environment_data else []
            process_environment: dict[str, str] = {}
            for item in environment_parts:
                key, separator, value = item.partition(b"=")
                decoded_key = key.decode()
                decoded_value = value.decode()
                if not separator or not decoded_key or decoded_key in process_environment:
                    raise RuntimeError("prepared Cursor process has malformed or duplicate environment fields")
                process_environment[decoded_key] = decoded_value
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"prepared Cursor process environment cannot be authenticated: {exc}") from exc
        expected_environment = dict(args.prepared_process_environment)
        if not expected_environment or process_environment != expected_environment:
            raise RuntimeError("prepared Cursor process did not inherit the exact sanitized launch environment")
        argv_sha256 = hashlib.sha256(b"\0".join(part.encode() for part in argv)).hexdigest()
        matches.append(CursorProcessProof(process.pid, executable, argv, argv_sha256))
    if len(matches) != 1:
        raise RuntimeError("prepared successor target does not contain exactly one exact installed Cursor runtime/argv process")
    return matches[0]


def cleanup_prepared_launch_window(window: LaunchWindow, args: Args | None = None) -> None:
    """Kill only the exact transaction pane, preserving every sibling pane."""

    if window.window_id and re.fullmatch(r"@[0-9]+", window.window_id) is None:
        raise RuntimeError("prepared launch cleanup has a malformed transaction-created window identity.")
    proven_window_id = window.window_id
    identity = tmux_for_args(args, ["display-message", "-p", "-t", window.pane_id, "#{session_id}\t#{window_id}\t#{pane_id}"])
    if identity.returncode == 0:
        fields = identity.stdout.strip().split("\t")
        if (
            len(fields) != 3
            or fields[0] != window.session_id
            or re.fullmatch(r"@[0-9]+", fields[1]) is None
            or (proven_window_id and fields[1] != proven_window_id)
            or fields[2] != window.pane_id
        ):
            raise RuntimeError("prepared launch failure cannot safely identify the transaction-created pane for cleanup.")
        proven_window_id = fields[1]
    else:
        inventory = tmux_for_args(args, ["list-panes", "-a", "-F", "#{session_id}\t#{window_id}\t#{pane_id}"])
        if inventory.returncode != 0:
            raise RuntimeError("prepared launch cleanup could not prove whether the created pane is present.")
        matches = [line for line in inventory.stdout.splitlines() if line.endswith(f"\t{window.pane_id}")]
        if not matches:
            return
        fields = matches[0].split("\t") if len(matches) == 1 else []
        if (
            len(fields) != 3
            or fields[0] != window.session_id
            or re.fullmatch(r"@[0-9]+", fields[1]) is None
            or (proven_window_id and fields[1] != proven_window_id)
            or fields[2] != window.pane_id
        ):
            raise RuntimeError("prepared launch cleanup alternate inventory did not prove the created pane identity.")
        proven_window_id = fields[1]
    siblings = tmux_for_args(args, ["list-panes", "-a", "-F", "#{session_id}\t#{window_id}\t#{pane_id}"])
    if siblings.returncode != 0:
        raise RuntimeError("prepared launch cleanup could not authenticate the complete pane inventory before cleanup.")
    before: dict[str, tuple[str, str]] = {}
    for line in siblings.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 3
            or TMUX_SESSION_ID_RE.fullmatch(fields[0]) is None
            or re.fullmatch(r"@[0-9]+", fields[1]) is None
            or re.fullmatch(r"%[0-9]+", fields[2]) is None
            or fields[2] in before
        ):
            raise RuntimeError("prepared launch cleanup received a malformed or duplicate pane inventory.")
        before[fields[2]] = (fields[0], fields[1])
    if before.get(window.pane_id) != (window.session_id, proven_window_id):
        raise RuntimeError("prepared launch cleanup inventory did not confirm the exact transaction pane identity.")
    preserved_siblings = {
        pane_id: pane_identity
        for pane_id, pane_identity in before.items()
        if pane_id != window.pane_id and pane_identity == (window.session_id, proven_window_id)
    }
    killed = tmux_for_args(args, ["kill-pane", "-t", window.pane_id])
    if killed.returncode != 0:
        raise RuntimeError(f"prepared launch failure could not kill its exact pane: {killed.stderr.strip()}")
    remaining = tmux_for_args(args, ["list-panes", "-a", "-F", "#{session_id}\t#{window_id}\t#{pane_id}"])
    if remaining.returncode != 0:
        raise RuntimeError("prepared launch cleanup could not prove exact pane absence after cleanup.")
    after: dict[str, tuple[str, str]] = {}
    for line in remaining.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 3
            or TMUX_SESSION_ID_RE.fullmatch(fields[0]) is None
            or re.fullmatch(r"@[0-9]+", fields[1]) is None
            or re.fullmatch(r"%[0-9]+", fields[2]) is None
            or fields[2] in after
        ):
            raise RuntimeError("prepared launch cleanup received a malformed post-cleanup pane inventory.")
        after[fields[2]] = (fields[0], fields[1])
    if window.pane_id in after:
        raise RuntimeError("prepared launch failure transaction pane is still live after exact cleanup.")
    if any(after.get(pane_id) != identity for pane_id, identity in preserved_siblings.items()):
        raise RuntimeError("prepared launch cleanup did not preserve an authenticated sibling pane identity.")


def prepared_exact_prompt(binding, config: dict[str, object], defaults_path: Path) -> bytes:
    from omo_manager.omo_worker_successor import read_frozen_prompt

    defaults = read_frozen_prompt(defaults_path)
    if hashlib.sha256(defaults.data).hexdigest() != config["worker_defaults_sha256"]:
        raise RuntimeError("prepared launch worker-default instruction bytes changed.")
    return defaults.data + b"\n" + manager_delegation(binding.prompt_data.decode(), binding.manager_target).encode()


def bound_prepared_launch_args(
    args: Args,
    *,
    prompt_file: Path,
    runtime: dict[str, object],
    shell_runtime: dict[str, object],
    environment: dict[str, object],
    process_environment: dict[str, object],
    tmux_runtime: dict[str, object],
    tmux_environment: dict[str, object],
) -> Args:
    return replace(
        args,
        prompt_file=prompt_file,
        prepared_runtime_path=Path(str(runtime["launcher_resolved"])),
        prepared_shell_path=Path(str(shell_runtime["bash_path"])),
        prepared_env_path=Path(str(shell_runtime["env_path"])),
        prepared_launch_environment=tuple(sorted((str(key), str(value)) for key, value in environment.items())),
        prepared_process_environment=tuple(sorted((str(key), str(value)) for key, value in process_environment.items())),
        prepared_tmux_path=Path(str(tmux_runtime["tmux_path"])),
        prepared_tmux_environment=tuple(sorted((str(key), str(value)) for key, value in tmux_environment.items())),
    )


def prepared_running_task_bytes(binding, launch_args: Args) -> bytes:
    running_text = launched_frontmatter_text(binding.successor_data.decode(), launch_args, binding.target)
    metadata = prepared_launch_metadata(running_text, binding.root)
    if metadata.status != "running" or metadata.pending_task_items != binding.queue:
        raise RuntimeError("prepared successor running state would not preserve its nonempty exact queue.")
    return running_text.encode()


def prepared_successor_launch(args: Args) -> tuple[Path, str]:
    """Launch one already-prepared blocked successor from exact committed bytes."""

    from omo_manager.omo_manager_replace import create_snapshot, read_snapshot, replace_snapshot
    from omo_manager.omo_worker_successor import (
        DEFAULT_WORKER_INSTRUCTIONS as PREPARED_WORKER_DEFAULTS,
        binding_from_committed_journal,
        canonical_target,
        cursor_runtime_identity,
        cursor_process_environment,
        minimal_launch_environment,
        minimal_tmux_environment,
        pinned_shell_identity,
        pinned_tmux_identity,
    )

    journal_path = args.prepared_successor_journal
    if journal_path is None:
        raise RuntimeError("prepared-successor launch requires its committed journal.")
    binding = binding_from_committed_journal(
        journal_path,
        expected_journal_sha256=args.expected_prepared_journal_sha256,
        expected_task_sha256=args.expected_prepared_task_sha256,
        expected_prompt_sha256=args.expected_prepared_prompt_sha256,
        expected_queue_sha256=args.expected_prepared_queue_sha256,
        expected_launch_manifest_sha256=args.expected_prepared_launch_manifest_sha256,
        verify_prelaunch_state=False,
    )
    requested_target = canonical_target(target(args))
    config = binding.launch_config
    if (
        args.root != binding.root
        or task_path(args.root, args.task_file) != binding.successor_path
        or requested_target != binding.target
        or canonical_target(args.manager_target) != binding.manager_target
        or effective_tool(args) != binding.tool
        or args.prompt_file is None
        or args.prompt_file.resolve(strict=False) != binding.prompt_path
        or args.tmux_session != config["tmux_session"]
        or args.tmux_window != config["tmux_window"]
        or args.workdir is None
        or str(args.workdir) != config["workdir"]
        or args.window_name != config["window_name"]
        or args.model != config["model"]
        or args.reasoning_effort != config["reasoning_effort"]
        or list(args.codex_flags) != config["codex_flags"]
        or args.amh_caller_agent != config["amh_caller_agent"]
        or args.prelaunch_source is not None
        or not args.no_link
        or args.is_manager
        or bool(args.session_id)
        or args.resume_idle
        or args.human_email_file is not None
        or args.human_email_lines is not None
        or args.human_email_text is not None
        or not args.require_existing_tmux_session
        or args.allow_new_tmux_session
        or args.dry_run
        or args.prepared_runtime_path is not None
        or args.prepared_shell_path is not None
        or args.prepared_env_path is not None
        or bool(args.prepared_launch_environment)
        or bool(args.prepared_process_environment)
        or args.prepared_tmux_path is not None
        or bool(args.prepared_tmux_environment)
    ):
        raise RuntimeError("prepared-successor launch arguments do not match the complete committed launch-manifest binding.")
    runtime = config.get("cursor_runtime")
    if not isinstance(runtime, dict) or cursor_runtime_identity() != runtime:
        raise RuntimeError("prepared successor installed Cursor runtime identity changed.")
    shell_runtime = config.get("shell_runtime")
    environment = config.get("environment")
    if not isinstance(shell_runtime, dict) or pinned_shell_identity() != shell_runtime:
        raise RuntimeError("prepared successor pinned shell/runtime identity changed.")
    if not isinstance(environment, dict) or minimal_launch_environment() != environment:
        raise RuntimeError("prepared successor minimal launch environment changed.")
    process_environment = config.get("cursor_process_environment")
    if (
        not isinstance(process_environment, dict)
        or args.workdir is None
        or cursor_process_environment(
            workdir=args.workdir,
            target=binding.target,
            amh_caller_agent=args.amh_caller_agent,
            runtime=runtime,
        )
        != process_environment
    ):
        raise RuntimeError("prepared successor exact Cursor process environment changed.")
    tmux_runtime = config.get("tmux_runtime")
    tmux_environment = config.get("tmux_environment")
    if not isinstance(tmux_runtime, dict) or pinned_tmux_identity() != tmux_runtime:
        raise RuntimeError("prepared successor pinned tmux client identity changed.")
    if not isinstance(tmux_environment, dict) or minimal_tmux_environment() != tmux_environment:
        raise RuntimeError("prepared successor minimal tmux environment changed.")
    receipt_path = prepared_launch_receipt_path(journal_path)
    lock_paths = (
        binding.successor_path,
        binding.old_path,
        binding.todo_path,
        binding.prompt_path,
        binding.launch_manifest_path,
        journal_path,
        receipt_path,
    )
    with root_membership_lock(binding.root), task_target_lock(binding.root, binding.target), ExitStack() as locks:
        for path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(path))
        binding = binding_from_committed_journal(
            journal_path,
            expected_journal_sha256=args.expected_prepared_journal_sha256,
            expected_task_sha256=args.expected_prepared_task_sha256,
            expected_prompt_sha256=args.expected_prepared_prompt_sha256,
            expected_queue_sha256=args.expected_prepared_queue_sha256,
            expected_launch_manifest_sha256=args.expected_prepared_launch_manifest_sha256,
            verify_prelaunch_state=False,
        )
        captured_prompt: Path | None = None
        exact_prompt = prepared_exact_prompt(binding, config, PREPARED_WORKER_DEFAULTS)
        launch_args = bound_prepared_launch_args(
            args,
            prompt_file=binding.prompt_path,
            runtime=runtime,
            shell_runtime=shell_runtime,
            environment=environment,
            process_environment=process_environment,
            tmux_runtime=tmux_runtime,
            tmux_environment=tmux_environment,
        )
        complete_inventory = prepared_tmux_pane_inventory(launch_args)
        protected_before = {
            target_name: next((identity for identity in complete_inventory.values() if identity.target == target_name), None)
            for target_name in binding.protected_targets
        }
        protected_sha256 = prepared_protected_inventory_sha256(protected_before)
        running_data = prepared_running_task_bytes(binding, launch_args)

        def finalize_recovered_launch(receipt_snapshot, receipt_record: PreparedLaunchReceipt, window: LaunchWindow) -> tuple[Path, str]:
            pane_id, pane_pid, _pane_command = prepared_launch_pane_identity(binding.target, launch_args, window.pane_id)
            if pane_pid != receipt_record.pane_pid:
                raise RuntimeError("prepared launch recovery pane process identity changed")
            process = prepared_cursor_process_proof(pane_id, launch_args, runtime, exact_prompt)
            if receipt_record.process_pid and (
                process.pid != receipt_record.process_pid or process.argv_sha256 != receipt_record.process_argv_sha256
            ):
                raise RuntimeError("prepared launch recovery Cursor process identity changed")
            task_snapshot = read_snapshot(binding.successor_path, "prepared successor recovery task")
            if task_snapshot.data == binding.successor_data:
                task_snapshot = replace_snapshot(task_snapshot, running_data, "prepared successor recovery task")
            elif task_snapshot.data != running_data:
                raise RuntimeError("prepared launch recovery found unknown successor task bytes")
            metadata = prepared_launch_metadata(task_snapshot.data.decode(), binding.root)
            if metadata.status != "running" or metadata.pending_task_items != binding.queue:
                raise RuntimeError("prepared launch recovery lost its exact nonempty queue")
            require_prepared_launch_sole_owner(binding)
            require_prepared_protected_inventory(binding, protected_sha256, launch_args)
            process = prepared_cursor_process_proof(pane_id, launch_args, runtime, exact_prompt)
            committed_data = prepared_launch_receipt_text(
                args,
                state="committed",
                target=binding.target,
                pane_id=pane_id,
                pane_pid=pane_pid,
                session_id=window.session_id,
                window_id=window.window_id,
                process_pid=process.pid,
                process_argv_sha256=process.argv_sha256,
                protected_inventory_sha256=protected_sha256,
            )
            if receipt_snapshot.data != committed_data:
                _ = replace_snapshot(receipt_snapshot, committed_data, "prepared-successor launch receipt")
            require_prepared_launch_sole_owner(binding)
            require_prepared_protected_inventory(binding, protected_sha256, launch_args)
            final_process = prepared_cursor_process_proof(pane_id, launch_args, runtime, exact_prompt)
            if final_process != process:
                raise RuntimeError("prepared launch recovery Cursor process identity changed after receipt commit")
            return binding.successor_path, binding.target
        if receipt_path.exists() or receipt_path.is_symlink():
            receipt = read_snapshot(receipt_path, "prepared-successor launch receipt")
            if stat.S_IMODE(receipt.state.st_mode) != 0o600 or receipt.state.st_uid != os.getuid():
                raise RuntimeError("prepared-successor launch receipt must remain owner-private mode 0600.")
            record = require_prepared_receipt_binding(receipt.data, args, binding, protected_sha256)
            if record.state == "failed":
                raise RuntimeError(f"the contained failed prepared launch receipt is preserved at {receipt_path}")
            if record.state == "prepared":
                current_task = read_snapshot(binding.successor_path, "prepared successor task")
                if current_task.data != binding.successor_data:
                    raise RuntimeError("prepared launch recovery found task publication without an attributable target")
                target_panes = [
                    identity for identity in prepared_tmux_pane_inventory(launch_args).values() if identity.target == binding.target
                ]
                require_prepared_launch_sole_owner(binding)
                require_prepared_protected_inventory(binding, protected_sha256, launch_args)
                if not target_panes:
                    prepared_receipt = receipt
                elif len(target_panes) == 1:
                    identity = target_panes[0]
                    pane_id, pane_pid, _command = prepared_launch_pane_identity(binding.target, launch_args, identity.pane_id)
                    inferred = PreparedLaunchReceipt(
                        "prepared",
                        binding.target,
                        pane_id,
                        pane_pid,
                        identity.session_id,
                        identity.window_id,
                        0,
                        "",
                        protected_sha256,
                    )
                    try:
                        return finalize_recovered_launch(
                            receipt,
                            inferred,
                            LaunchWindow(binding.target, pane_id, identity.session_id, window_id=identity.window_id),
                        )
                    except Exception as exc:
                        failed = prepared_launch_receipt_text(
                            args,
                            state="failed",
                            target=binding.target,
                            pane_id=pane_id,
                            pane_pid=pane_pid,
                            session_id=identity.session_id,
                            window_id=identity.window_id,
                            protected_inventory_sha256=protected_sha256,
                            error=f"unattributed post-create target preserved without cleanup: {exc}",
                        )
                        _ = replace_snapshot(receipt, failed, "prepared-successor launch receipt")
                        raise RuntimeError(
                            "prepared launch recovery preserved an unattributable post-create target without killing it"
                        ) from exc
                else:
                    raise RuntimeError("prepared launch recovery found multiple panes for its exact target")
            else:
                window = authenticated_prepared_launch_window(record, launch_args)
                if record.state == "committed":
                    current_task = read_snapshot(binding.successor_path, "committed successor task")
                    if current_task.data != running_data:
                        raise RuntimeError("committed prepared successor task bytes changed")
                    result = finalize_recovered_launch(receipt, record, window)
                    expected = prepared_launch_receipt_text(
                        args,
                        state="committed",
                        target=binding.target,
                        pane_id=record.pane_id,
                        pane_pid=record.pane_pid,
                        session_id=record.session_id,
                        window_id=record.window_id,
                        process_pid=record.process_pid,
                        process_argv_sha256=record.process_argv_sha256,
                        protected_inventory_sha256=protected_sha256,
                    )
                    if read_snapshot(receipt_path, "committed launch receipt").data != expected:
                        raise RuntimeError("committed prepared launch receipt bytes changed")
                    return result
                try:
                    return finalize_recovered_launch(receipt, record, window)
                except Exception as exc:
                    cleanup_failures: list[str] = []
                    try:
                        task_snapshot = read_snapshot(binding.successor_path, "incomplete successor task")
                        if task_snapshot.data == running_data:
                            _ = replace_snapshot(task_snapshot, binding.successor_data, "incomplete successor task")
                        elif task_snapshot.data != binding.successor_data:
                            cleanup_failures.append("successor task has unknown bytes")
                    except Exception as task_error:
                        cleanup_failures.append(f"task containment failed: {task_error}")
                    try:
                        cleanup_prepared_launch_window(window, launch_args)
                    except Exception as window_error:
                        cleanup_failures.append(f"window containment failed: {window_error}")
                    detail = str(exc)
                    if cleanup_failures:
                        detail += "; " + "; ".join(cleanup_failures)
                    failed = prepared_launch_receipt_text(
                        args,
                        state="failed",
                        target=binding.target,
                        pane_id=record.pane_id,
                        pane_pid=record.pane_pid,
                        session_id=record.session_id,
                        window_id=record.window_id,
                        process_pid=record.process_pid,
                        process_argv_sha256=record.process_argv_sha256,
                        protected_inventory_sha256=protected_sha256,
                        error=detail,
                    )
                    _ = replace_snapshot(receipt, failed, "prepared-successor launch receipt")
                    raise RuntimeError("incomplete prepared launch was task-bound and contained; its failed receipt is preserved") from exc
        else:
            current_task = read_snapshot(binding.successor_path, "prepared successor task")
            if current_task.data != binding.successor_data:
                raise RuntimeError("prepared successor task bytes changed before launch preparation")
            current_metadata = prepared_launch_metadata(current_task.data.decode(), binding.root)
            if current_metadata.status != "blocked" or current_metadata.pending_task_items != binding.queue:
                raise RuntimeError("prepared successor is not blocked with its exact nonempty queue")
            require_prepared_launch_sole_owner(binding)
            if any(identity.target == binding.target for identity in prepared_tmux_pane_inventory(launch_args).values()):
                raise RuntimeError("prepared successor target appeared before durable launch preparation")
            require_prepared_protected_inventory(binding, protected_sha256, launch_args)
            prepared_receipt = create_snapshot(
                receipt_path,
                prepared_launch_receipt_text(
                    args,
                    state="prepared",
                    target=binding.target,
                    protected_inventory_sha256=protected_sha256,
                ),
                0o600,
            )
        window: LaunchWindow | None = None
        running_snapshot = None
        pane_pid = 0
        process: CursorProcessProof | None = None
        current_receipt = prepared_receipt
        try:
            if any(identity.target == binding.target for identity in prepared_tmux_pane_inventory(launch_args).values()):
                raise RuntimeError("prepared successor target appeared before launch.")
            require_prepared_launch_sole_owner(binding)
            require_prepared_protected_inventory(binding, protected_sha256, launch_args)
            with tempfile.NamedTemporaryFile("wb", prefix="omo-prepared-successor-prompt-", delete=False) as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(exact_prompt)
                handle.flush()
                os.fsync(handle.fileno())
                captured_prompt = Path(handle.name)
            launch_args = replace(launch_args, prompt_file=captured_prompt)
            window = new_window_bound(launch_args)
            if canonical_target(window.target) != binding.target:
                raise RuntimeError("tmux allocated a target different from the prepared successor binding.")
            verify_launch_window(window, launch_args)
            pane_id, pane_pid, _pane_command = prepared_launch_pane_identity(binding.target, launch_args, window.pane_id)
            current_receipt = replace_snapshot(
                current_receipt,
                prepared_launch_receipt_text(
                    args,
                    state="window",
                    target=binding.target,
                    pane_id=pane_id,
                    pane_pid=pane_pid,
                    session_id=window.session_id,
                    window_id=window.window_id,
                    protected_inventory_sha256=protected_sha256,
                ),
                "prepared-successor launch receipt",
            )
            maybe_crash_prepared_launch("window")
            start_codex(window.target, launch_args)
            pane_id, pane_pid, _pane_command = prepared_launch_pane_identity(binding.target, launch_args, window.pane_id)
            process = prepared_cursor_process_proof(pane_id, launch_args, runtime, exact_prompt)
            maybe_crash_prepared_launch("cursor-started-unrecorded")
            current_receipt = replace_snapshot(
                current_receipt,
                prepared_launch_receipt_text(
                    args,
                    state="started",
                    target=binding.target,
                    pane_id=pane_id,
                    pane_pid=pane_pid,
                    session_id=window.session_id,
                    window_id=window.window_id,
                    process_pid=process.pid,
                    process_argv_sha256=process.argv_sha256,
                    protected_inventory_sha256=protected_sha256,
                ),
                "prepared-successor launch receipt",
            )
            maybe_crash_prepared_launch("started")
            current_task = read_snapshot(binding.successor_path, "prepared successor task")
            if current_task.data != binding.successor_data:
                raise RuntimeError("prepared successor task changed before running-state publication.")
            atomic_replace_if_unchanged(binding.successor_path, running_data.decode(), current_task.state, lock_held=True)
            final_task = read_snapshot(binding.successor_path, "running successor task")
            if final_task.data != running_data:
                raise RuntimeError("prepared successor running task bytes changed during publication")
            running_snapshot = final_task
            maybe_crash_prepared_launch("task-published")
            current_receipt = replace_snapshot(
                current_receipt,
                prepared_launch_receipt_text(
                    args,
                    state="published",
                    target=binding.target,
                    pane_id=pane_id,
                    pane_pid=pane_pid,
                    session_id=window.session_id,
                    window_id=window.window_id,
                    process_pid=process.pid,
                    process_argv_sha256=process.argv_sha256,
                    protected_inventory_sha256=protected_sha256,
                ),
                "prepared-successor launch receipt",
            )
            maybe_crash_prepared_launch("published")
            final_metadata = prepared_launch_metadata(final_task.data.decode(), args.root)
            if final_metadata.status != "running" or final_metadata.pending_task_items != binding.queue:
                raise RuntimeError("prepared successor launch postconditions lost the running nonempty exact queue.")
            require_prepared_launch_sole_owner(binding)
            require_prepared_protected_inventory(binding, protected_sha256, launch_args)
            pane_id, pane_pid, _pane_command = prepared_launch_pane_identity(binding.target, launch_args, window.pane_id)
            process = prepared_cursor_process_proof(pane_id, launch_args, runtime, exact_prompt)
            current_receipt = replace_snapshot(
                current_receipt,
                prepared_launch_receipt_text(
                    args,
                    state="committed",
                    target=binding.target,
                    pane_id=pane_id,
                    pane_pid=pane_pid,
                    session_id=window.session_id,
                    window_id=window.window_id,
                    process_pid=process.pid,
                    process_argv_sha256=process.argv_sha256,
                    protected_inventory_sha256=protected_sha256,
                ),
                "prepared-successor launch receipt",
            )
            require_prepared_launch_sole_owner(binding)
            require_prepared_protected_inventory(binding, protected_sha256, launch_args)
            final_process = prepared_cursor_process_proof(pane_id, launch_args, runtime, exact_prompt)
            if final_process != process:
                raise RuntimeError("prepared successor Cursor process identity changed after receipt commit")
            return binding.successor_path, binding.target
        except Exception as exc:
            cleanup_failures: list[str] = []
            if running_snapshot is not None:
                try:
                    current = read_snapshot(binding.successor_path, "failed running successor task")
                    if current.data == running_snapshot.data:
                        _ = replace_snapshot(current, binding.successor_data, "failed running successor task")
                    elif current.data != binding.successor_data:
                        cleanup_failures.append("successor task changed after running-state publication")
                except Exception as rollback_error:
                    cleanup_failures.append(f"task rollback failed: {rollback_error}")
            if window is not None:
                try:
                    cleanup_prepared_launch_window(window, launch_args)
                except Exception as window_error:
                    cleanup_failures.append(f"window cleanup failed: {window_error}")
            if cleanup_failures:
                exc.add_note("; ".join(cleanup_failures))
            try:
                _ = replace_snapshot(
                    current_receipt,
                    prepared_launch_receipt_text(
                        args,
                        state="failed",
                        target=binding.target,
                        pane_id=window.pane_id if window is not None else "",
                        pane_pid=pane_pid,
                        session_id=window.session_id if window is not None else "",
                        window_id=window.window_id if window is not None else "",
                        process_pid=process.pid if process is not None else 0,
                        process_argv_sha256=process.argv_sha256 if process is not None else "",
                        protected_inventory_sha256=protected_sha256,
                        error=str(exc),
                    ),
                    "prepared-successor launch receipt",
                )
            except Exception as receipt_error:
                exc.add_note(f"prepared launch receipt finalization also failed: {receipt_error}")
            raise
        finally:
            if captured_prompt is not None:
                captured_prompt.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.migrate_manager_owner:
            migration_path = task_path(args.root, args.task_file)
            migration_text = migration_path.read_text(encoding="utf-8") if migration_path.is_file() else ""
            _ = migration_source_metadata(migration_text, args.root) if migration_text else None
            source = frontmatter_text(migration_text)
            migration_version = first_version(source) if source is not None else ""
            if migration_version == TASK_FRONTMATTER_V2 and not v2_enabled(args.root):
                raise ValueError("v2 ownership migration is disabled until reviewed migration enablement is complete.")
            if migration_version == TASK_FRONTMATTER_V1 and v2_enabled(args.root):
                raise ValueError("v1 task writes are disabled after v2 enablement.")
            migrate_manager_owner(migration_path, args.old_manager_target, args.new_manager_target, args.dry_run, args.root)
            return 0
        if args.prepared_successor_journal is not None:
            if args.dry_run:
                raise ValueError("prepared-successor launch does not support --dry-run; validate the committed preparation directly.")
            path, tmux_target = prepared_successor_launch(args)
            print(path)
            print(tmux_target)
            print("prepared successor launch committed with exact task, prompt, queue, process, and sole-owner bindings")
            return 0
        existing_target = target(args) if args.workdir is None else ""
        with root_membership_lock(args.root):
            ownership_lock = task_target_lock(args.root, existing_target) if existing_target else contextlib.nullcontext()
            with ownership_lock:
                args = replace(args, human_email_text=validate_inputs(args))
                existing_pane_id = validate_existing_target_runtime(args)
                if args.dry_run:
                    dry_run(args)
                    return 0
                existed = task_path(args.root, args.task_file).exists()
                launch_target = new_window(args)
                tmux_target = str(launch_target)
                if existing_pane_id and exact_pane_id(tmux_target) != existing_pane_id:
                    raise ValueError(f"existing target `{tmux_target}` changed before task registration; retry or use --workdir to launch a new worker.")
                if args.workdir is not None and isinstance(launch_target, LaunchTarget):
                    verify_launch_window(
                        LaunchWindow(
                            tmux_target,
                            launch_target.pane_id,
                            launch_target.session_id,
                            launch_target.created_session,
                            launch_target.session_name,
                        )
                    )
                path = ensure_task_file(args, tmux_target)
                if not args.no_link:
                    link_todo(args, tmux_target)
            if args.workdir is not None:
                start_codex(tmux_target, args)
        print(path)
        if tmux_target:
            print(tmux_target)
        if not existed:
            print("reminder: the launched agent owns its open-work queue through omo_pending.py; do not pass task paths to it.")
        if args.workdir is not None and not args.resume_idle:
            print("reminder: launch verification succeeded; wait patiently for the agent to report instead of eagerly checking its status.")
    except Exception as exc:
        print(f"omo_task: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
