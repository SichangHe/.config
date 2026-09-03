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
import json
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
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

try:
    from omo_manager.omo_codex_status import Args as StatusArgs
    from omo_manager.omo_codex_status import CODEX_FOOTER_RE, current_block, current_input_text, exact_tail, inspect, is_stock_placeholder_input_text, report_from_lines, tail, visible_error_lines
    from omo_manager.omo_codex_status import status as classify_status
    from omo_manager.omo_codex_stop import extract_new_status_session_id, query_status_session_id
    from omo_manager.omo_task_lock import task_file_lock, task_target_lock
    from omo_manager.omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_STATUSES, frontmatter_parts, parse_task_metadata
    from omo_manager.omo_task_status import authoritative_active_target_task_paths, root_membership_lock
except ModuleNotFoundError:
    from omo_codex_status import Args as StatusArgs
    from omo_codex_status import CODEX_FOOTER_RE, current_block, current_input_text, exact_tail, inspect, is_stock_placeholder_input_text, report_from_lines, tail, visible_error_lines
    from omo_codex_status import status as classify_status
    from omo_codex_stop import extract_new_status_session_id, query_status_session_id
    from omo_task_lock import task_file_lock, task_target_lock
    from omo_task_metadata import TARGET_RE, TASK_FRONTMATTER_STATUSES, frontmatter_parts, parse_task_metadata
    from omo_task_status import authoritative_active_target_task_paths, root_membership_lock  # pyright: ignore[reportImplicitRelativeImport]

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
HCFG_RESTART_ROOT = Path("/ssd1/sichangheagent/work_logs")
HCFG_RESTART_TASK_FILE = "helper_audit_human_facing.md"
HCFG_RESTART_AUTHORITY_FILE = Path("manager_mail/85c5dff58359-1375.txt")
HCFG_RESTART_AUTHORITY_LINES = (3, 7)
HCFG_RESTART_AUTHORITY_SHA256 = "d7bff25be32f089e63d1a06c41d0e433fca75ee7eee14bdfd13a6f85b2b45977"
HCFG_RESTART_AUTHORITY_TEXT = """For manager
That’s none of this agent's business. Their task is to fix the manager infra
Re-follow getagentsmd
Find the original human prompts for this agent and collect only original words to suit a starting prompt
Then restart hcfg:1 in place"""
HCFG_RESTART_TARGET = "hcfg:1.0"
SOURCE1206_ROOT = Path("/ssd1/sichangheagent/work_logs")
SOURCE1206_AUTHORITY_FILE = Path("manager_mail/85c5dff58359-1206.txt")
SOURCE1206_AUTHORITY_LINES = (3, 13)
SOURCE1206_AUTHORITY_SHA256 = "47b1830fae4f3cee23aa8c9655eff3be613dea85c2b1a842979756d33b4c9268"
SOURCE1206_APPROVAL = "Option one is not a real option, do it agientically and override the regular rules."
SOURCE1206_PROCEDURE = "> Option 2: approve one one-time exception: after the restart, send only /status to learn which process is answering. /status is a read-only status request; it cannot run the experiment, alter files, call a model, or spend money. The risk is that old terminal routing could send this status request to the old process. If the reply is old, missing, or ambiguous, the process will be stopped and no work instruction will be sent. An independent reviewer will approve the exact procedure before it is tried."
SOURCE1206_SCOPE = "> Please reply with either “wait” or “allow the one-time status check.” This approval would apply only to this one replacement attempt."
SOURCE1206_TASK_FILE = "dw1113_bedrock.md"
SOURCE1206_TARGET = "dw5:0.0"
SOURCE1206_AUDIT_PATH = (Path.home() / ".local/state/omo-manager/rotations/worker-rotation-source1183-1206.audit").resolve(strict=False)
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
CODEX_PACKAGE = "@openai/codex@latest"
SUPPORTED_CODEX_PACKAGES = {"@openai/codex", CODEX_PACKAGE}
SUPPORTED_CODEX_PROCESS_COMMANDS = {"bun", "bunx", "codex"}
ROTATION_AUDIT_MAX_BYTES = 64 * 1024
RECONCILABLE_ROTATION_FAILURE_KIND = "post-respawn-new-session-id-capture-failed"
ROTATION_ELIGIBILITY_XATTR = "user.omo_rotation_reconciliation_eligible_sha256"
PCODX_LAUNCH_COMMAND = str(HELPER_DIR / "pcodx")
UPDATE_AVAILABLE_RE = re.compile(r"^✨\s*Update available! [0-9]+\.[0-9]+\.[0-9]+ -> [0-9]+\.[0-9]+\.[0-9]+$")
UPDATE_PROMPT_SUFFIX = (
    "Release notes: https://github.com/openai/codex/releases/latest",
    "› 1. Update now (runs `bun install -g @openai/codex`)",
    "2. Skip",
    "3. Skip until next version",
    "Press enter to continue",
)
RESUME_CWD_PROMPT_PREFIX = (
    "Choose working directory to resume this session",
    "Session = latest cwd recorded in the resumed session",
    "Current = your current working directory",
)
RESUME_CWD_SESSION_RE = re.compile(r"^› 1\. Use session directory \((?P<path>/.*)\)$")
RESUME_CWD_CURRENT_RE = re.compile(r"^2\. Use current directory \((?P<path>/.*)\)$")
RESUME_CWD_PROMPT_SUFFIX = ("3. Always use session directory", "4. Always use current directory", "Press enter to continue")
RESUME_CWD_IGNORABLE_MCP_ERRORS = (
    "⚠ MCP client for `codex_apps` failed to start: MCP startup failed: Transport",
    "⚠ MCP startup incomplete (failed: codex_apps)",
)


class StartError(RuntimeError):
    """A same-pane launch precondition or operation failed."""


class RotationSessionCaptureFailed(StartError):
    """Fresh rotation evidence was absent, stale-only, or proved same-old."""

    def __init__(self, message: str, failure_kind: str, response_sha256: str = "", captured_session_id: str = "") -> None:
        super().__init__(message)
        self.failure_kind = failure_kind
        self.response_sha256 = response_sha256
        self.captured_session_id = captured_session_id


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
    assert_legacy_missing_session_id: bool = False
    stop_unverified_replacement: bool = False
    expected_blocker: str | None = None
    reconcile_rotation_audit: bool = False
    rotation_audit: Path | None = None
    expected_rotation_audit_sha256: str = ""
    reconciliation_receipt: Path | None = None
    expected_current_pane_pid: int = 0
    expected_current_command: str = ""
    recover_resume_cwd_prompt: bool = False
    resume_cwd_choice: str = ""
    expected_session_directory: Path | None = None
    retire_recovery_evidence: bool = False


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
    blocked_on: str
    task_sha256: str


# 🧑 Manager delegation: "replace separate rotation identity probes with one stable atomic snapshot"
@dataclass(frozen=True)
class RotationSnapshot:
    pane: Pane
    task: TaskBinding
    old_session_id: str
    task_path: Path
    protected_targets: tuple[str, ...]
    audit_path: Path
    todo_sha256: str
    sha256: str


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


@dataclass(frozen=True)
class HumanRestartSpec:
    root: Path
    task_file: str
    authority_file: Path
    authority_lines: tuple[int, int]
    authority_sha256: str
    authority_text: str
    target: str


@dataclass(frozen=True)
class Source1206Authority:
    source_path: Path
    source_lines: tuple[int, int]
    source_dev: int
    source_inode: int
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    target: str
    pane_id: str
    window_id: str
    pane_pid: int


@dataclass(frozen=True)
class RotationAuditBinding:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    content: bytes
    text: str
    sha256: str
    fields: Mapping[str, str]


@dataclass(frozen=True)
class ReconciliationBinding:
    audit: RotationAuditBinding
    task: TaskBinding
    target: str
    pane_id: str
    window_id: str
    pane_pid: int
    command: str
    todo_device: int
    todo_inode: int
    todo_size: int
    todo_mtime_ns: int
    todo_sha256: str


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, default=Path(os.environ.get("OMO_WORK_LOGS_ROOT", Path.home() / "work_logs")))
    _ = parser.add_argument("--task-file", required=True, help="Active tracked task whose `runat` names the target pane.")
    _ = parser.add_argument("--target", required=True, help="Exact existing pane: SESSION:WINDOW[.PANE].")
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
    _ = parser.add_argument("--rotate-worker", action="store_true", help="Replace one exact live non-manager Codex worker with a fresh context in the same pane.")
    _ = parser.add_argument("--expected-task-sha256", help="Expected SHA-256 of the tracked task bytes; required with --rotate-worker.")
    _ = parser.add_argument("--expected-status", choices=sorted(ROTATION_TASK_STATUSES), help="Expected preserved task status; required with --rotate-worker.")
    _ = parser.add_argument("--expected-owner-target", help="Expected preserved task manager target; required with --rotate-worker.")
    _ = parser.add_argument("--expected-pending-item", action="append", default=[], help="Exact pending item in order; repeat for the full preserved queue with --rotate-worker.")
    _ = parser.add_argument("--protected-target", action="append", default=[], help="Target that this rotation must not touch; repeat the authoritative protected set with --rotate-worker.")
    _ = parser.add_argument("--audit-output", type=Path, help="New owner-private audit file; required with --rotate-worker.")
    _ = parser.add_argument(
        "--assert-legacy-missing-session-id",
        action="store_true",
        help="Assert that this legacy worker's old Codex UUID is unrecoverable; valid only with --rotate-worker and --expected-blocker.",
    )
    _ = parser.add_argument(
        "--stop-unverified-replacement",
        action="store_true",
        help="Stop the exact replacement into an empty shell if fresh UUID proof fails; valid only with --rotate-worker.",
    )
    _ = parser.add_argument("--expected-blocker", help="Exact preserved lifecycle blocker; required only with --assert-legacy-missing-session-id.")
    _ = parser.add_argument("--reconcile-rotation-audit", action="store_true", help="Record later UUID evidence without launching; the sole input is one identity-guarded `/status` query.")
    _ = parser.add_argument("--rotation-audit", type=Path, help="Exact existing failed rotation audit for --reconcile-rotation-audit.")
    _ = parser.add_argument("--expected-rotation-audit-sha256", help="Expected lowercase SHA-256 of the failed rotation audit.")
    _ = parser.add_argument("--reconciliation-receipt", type=Path, help="New owner-private receipt path for later reconciliation evidence.")
    _ = parser.add_argument("--expected-current-pane-pid", type=int, help="Exact current pane process id asserted for reconciliation.")
    _ = parser.add_argument("--expected-current-command", help="Exact supported current Codex process command asserted for reconciliation.")
    _ = parser.add_argument(
        "--recover-update-prompt",
        action="store_true",
        help="Select Skip only in Codex's exact startup update menu for the supplied resumed session.",
    )
    _ = parser.add_argument(
        "--recover-resume-cwd-prompt",
        action="store_true",
        help="Select a nonpersistent directory choice only in Codex's exact resume working-directory menu.",
    )
    _ = parser.add_argument("--resume-cwd-choice", choices=("current", "session"), help="Nonpersistent directory choice for --recover-resume-cwd-prompt.")
    _ = parser.add_argument("--expected-session-directory", type=Path, help="Exact saved session directory shown by --recover-resume-cwd-prompt.")
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
        "--retire-recovery-evidence",
        action="store_true",
        help="Retry cleanup of a verified recovery transaction from its durable retirement manifest; does not inspect or modify tmux.",
    )
    _ = parser.add_argument(
        "--record-recovery-evidence",
        action="store_true",
        help="Record a one-use failed-delivery receipt after verifying this pane; does not launch Codex.",
    )
    _ = parser.add_argument("--recovery-output", type=Path, help="Watcher-issued receipt path (root/.omo-codex-recovery-receipts/<delivery-id>.receipt).")
    _ = parser.add_argument(
        "--failed-delivery-id",
        help="Watcher delivery-event id for --record-recovery-evidence; a 16-character agent-problem id is not valid.",
    )
    _ = parser.add_argument("--human-email-file", type=Path, help="Authoritative human email under ROOT/manager_mail for an h* same-pane restart.")
    _ = parser.add_argument("--human-email-lines", help="Inclusive source line range START-END for --human-email-file.")
    _ = parser.add_argument("--dry-run", action="store_true")
    parsed = parser.parse_args(argv)
    if parsed.model and MODEL_RE.fullmatch(parsed.model) is None:
        parser.error("--model contains unsupported characters.")
    if parsed.model == "gpt-5.6":
        parser.error("--model gpt-5.6 is not a supported Codex model id; use gpt-5.6-sol, gpt-5.6-terra, or gpt-5.6-luna.")
    if parsed.session_id and UUID_RE.fullmatch(parsed.session_id) is None:
        parser.error("--session-id must be a Codex UUID.")
    modes = (
        parsed.restart_running,
        parsed.rotate_worker,
        parsed.recover_non_codex,
        parsed.record_recovery_evidence,
        parsed.recover_update_prompt,
        parsed.recover_resume_cwd_prompt,
        parsed.reconcile_rotation_audit,
        parsed.retire_recovery_evidence,
    )
    if sum(bool(value) for value in modes) > 1:
        parser.error("launch, recovery, rotation, and audit reconciliation modes are mutually exclusive.")
    if not (parsed.reconcile_rotation_audit or parsed.retire_recovery_evidence) and (not parsed.model or not parsed.reasoning_effort):
        parser.error("--model and --reasoning-effort are required for launch and recovery modes.")
    if parsed.restart_running and (parsed.prompt_file or parsed.session_id):
        parser.error("--restart-running captures the live session and does not accept --prompt-file or --session-id.")
    if parsed.rotate_worker or parsed.reconcile_rotation_audit:
        if parsed.prompt_file or parsed.session_id:
            parser.error("rotation and reconciliation modes do not accept --prompt-file or --session-id.")
        if not all((parsed.expected_task_sha256, parsed.expected_status, parsed.expected_owner_target, parsed.expected_pending_item, parsed.protected_target)):
            parser.error("rotation and reconciliation require task digest, status, owner, full nonempty pending queue, and protected-target assertions.")
        if SHA256_RE.fullmatch(parsed.expected_task_sha256) is None:
            parser.error("--expected-task-sha256 must be a lowercase SHA-256 value.")
        if any(target_identity(target) is None for target in parsed.protected_target):
            parser.error("--protected-target values must be exact SESSION:WINDOW[.PANE] targets.")
        if parsed.rotate_worker and not parsed.audit_output:
            parser.error("--rotate-worker requires --audit-output.")
        if parsed.rotate_worker and parsed.assert_legacy_missing_session_id != (parsed.expected_blocker is not None):
            parser.error("--assert-legacy-missing-session-id and --expected-blocker must be supplied together.")
        if parsed.reconcile_rotation_audit:
            if parsed.expected_blocker is None or not all((parsed.rotation_audit, parsed.expected_rotation_audit_sha256, parsed.reconciliation_receipt, parsed.expected_current_pane_pid, parsed.expected_current_command)):
                parser.error("--reconcile-rotation-audit requires original audit/hash, receipt, blocker, current pane pid/command, and all task assertions.")
            if SHA256_RE.fullmatch(parsed.expected_rotation_audit_sha256) is None:
                parser.error("--expected-rotation-audit-sha256 must be a lowercase SHA-256 value.")
            if parsed.expected_current_pane_pid <= 0:
                parser.error("--expected-current-pane-pid must be positive.")
            if parsed.expected_current_command not in SUPPORTED_CODEX_PROCESS_COMMANDS:
                parser.error("--expected-current-command must name a supported Codex process.")
            if parsed.audit_output or parsed.assert_legacy_missing_session_id or parsed.stop_unverified_replacement:
                parser.error("rotation mutation assertions are invalid with --reconcile-rotation-audit.")
            if parsed.dry_run:
                parser.error("--dry-run is invalid with --reconcile-rotation-audit.")
    elif any(
        (
            parsed.expected_task_sha256,
            parsed.expected_status,
            parsed.expected_owner_target,
            parsed.expected_pending_item,
            parsed.protected_target,
            parsed.audit_output,
            parsed.assert_legacy_missing_session_id,
            parsed.expected_blocker is not None,
            parsed.stop_unverified_replacement,
            parsed.rotation_audit,
            parsed.expected_rotation_audit_sha256,
            parsed.reconciliation_receipt,
            parsed.expected_current_pane_pid,
            parsed.expected_current_command,
        )
    ):
        parser.error("rotation assertions are only valid with --rotate-worker.")
    if parsed.recover_non_codex and parsed.session_id:
        parser.error("--recover-non-codex launches a fresh session and does not accept --session-id.")
    if parsed.recover_non_codex and not parsed.prompt_file:
        parser.error("--recover-non-codex requires --prompt-file for the recorded task/prompt context.")
    if parsed.recover_non_codex and not parsed.recovery_evidence:
        parser.error("--recover-non-codex requires --recovery-evidence.")
    if parsed.retire_recovery_evidence and not parsed.recovery_evidence:
        parser.error("--retire-recovery-evidence requires --recovery-evidence.")
    if parsed.retire_recovery_evidence and parsed.dry_run:
        parser.error("--dry-run is invalid with --retire-recovery-evidence.")
    if parsed.record_recovery_evidence and (parsed.session_id or parsed.prompt_file):
        parser.error("--record-recovery-evidence records the pane only and does not accept --session-id or --prompt-file.")
    if parsed.record_recovery_evidence and not parsed.recovery_output:
        parser.error("--record-recovery-evidence requires --recovery-output.")
    if parsed.record_recovery_evidence and not parsed.failed_delivery_id:
        parser.error("--record-recovery-evidence requires --failed-delivery-id.")
    if parsed.recover_update_prompt and (not parsed.session_id or parsed.prompt_file):
        parser.error("--recover-update-prompt requires --session-id and does not accept --prompt-file.")
    if parsed.recover_resume_cwd_prompt and (not parsed.session_id or parsed.prompt_file):
        parser.error("--recover-resume-cwd-prompt requires --session-id and does not accept --prompt-file.")
    if parsed.recover_resume_cwd_prompt and (not parsed.resume_cwd_choice or parsed.expected_session_directory is None):
        parser.error("--recover-resume-cwd-prompt requires --resume-cwd-choice and --expected-session-directory.")
    if (parsed.resume_cwd_choice or parsed.expected_session_directory is not None) and not parsed.recover_resume_cwd_prompt:
        parser.error("--resume-cwd-choice and --expected-session-directory require --recover-resume-cwd-prompt.")
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
    if parsed.human_email_file is not None and not (parsed.restart_running or parsed.stop_unverified_replacement):
        parser.error("human email authority is only valid with --restart-running or --stop-unverified-replacement.")
    if parsed.stop_unverified_replacement and parsed.human_email_file is None:
        parser.error("--stop-unverified-replacement requires its exact Source-1206 human email authority.")
    if not any(modes) and bool(parsed.session_id) == bool(parsed.prompt_file):
        parser.error("provide exactly one of --session-id or --prompt-file.")
    if parsed.recovery_evidence and not (parsed.recover_non_codex or parsed.retire_recovery_evidence):
        parser.error("--recovery-evidence is only valid with --recover-non-codex or --retire-recovery-evidence.")
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
        assert_legacy_missing_session_id=parsed.assert_legacy_missing_session_id,
        stop_unverified_replacement=parsed.stop_unverified_replacement,
        expected_blocker=parsed.expected_blocker,
        reconcile_rotation_audit=parsed.reconcile_rotation_audit,
        rotation_audit=Path(os.path.abspath(parsed.rotation_audit.expanduser())) if parsed.rotation_audit else None,
        expected_rotation_audit_sha256=parsed.expected_rotation_audit_sha256 or "",
        reconciliation_receipt=Path(os.path.abspath(parsed.reconciliation_receipt.expanduser())) if parsed.reconciliation_receipt else None,
        expected_current_pane_pid=parsed.expected_current_pane_pid or 0,
        expected_current_command=parsed.expected_current_command or "",
        recover_resume_cwd_prompt=parsed.recover_resume_cwd_prompt,
        resume_cwd_choice=parsed.resume_cwd_choice or "",
        expected_session_directory=parsed.expected_session_directory.expanduser().resolve(strict=False) if parsed.expected_session_directory else None,
        retire_recovery_evidence=parsed.retire_recovery_evidence,
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
    root_path = root.resolve()
    path = (root_path / task_file).resolve()
    if path == root_path or not path.is_relative_to(root_path):
        raise StartError("--task-file must name one file under --root.")
    return path


def task_ref(root: Path, path: Path) -> str:
    return path.relative_to(root.resolve()).as_posix()


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


def human_pending_todo_entries(text: str) -> set[str]:
    section = ""
    entries: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.rstrip(":").casefold() in {"current", "previous", "human pending", "low priority"}:
            section = stripped.rstrip(":").casefold()
        elif section == "human pending" and stripped:
            entries.add(stripped)
    return entries


def validate_task(args: Args, pane: Pane, *, verify_target: bool = True, allow_human_pending: bool = False) -> TaskBinding:
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
        if args.assert_legacy_missing_session_id and metadata.blocked_on != args.expected_blocker:
            raise StartError("task blocker does not equal --expected-blocker.")
        if hashlib.sha256(task_bytes).hexdigest() != args.expected_task_sha256:
            raise StartError("task bytes do not equal --expected-task-sha256.")
    if verify_target and resolve_pane(metadata.runat).pane_id != pane.pane_id:
        raise StartError(f"task `runat` {metadata.runat} does not identify target {pane.target}.")
    todo = args.root / "TODO.md"
    expected = f"{task_ref(args.root, path)} {metadata.runat}"
    todo_text = todo.read_text(encoding="utf-8") if todo.is_file() else ""
    accepted_entries = current_todo_entries(todo_text)
    if allow_human_pending:
        accepted_entries |= human_pending_todo_entries(todo_text)
    if expected not in accepted_entries:
        section = "`current` or `human pending`" if allow_human_pending else "`current`"
        raise StartError(f"TODO {section} does not contain exact task entry: {expected}")
    return TaskBinding(
        metadata.is_manager,
        metadata.tool,
        metadata.status,
        metadata.runat,
        metadata.managerat,
        metadata.pending_task_items,
        metadata.blocked_on,
        hashlib.sha256(task_bytes).hexdigest(),
    )


def human_restart_source(args: Args, spec: HumanRestartSpec) -> tuple[Path, os.stat_result, str]:
    """Read one byte-exact private email authorized for one protected pane."""

    if args.human_email_file is None or args.human_email_lines is None:
        raise StartError("human-owned pane restart requires --human-email-file and --human-email-lines.")
    try:
        approved_root = spec.root.resolve(strict=True)
    except OSError as error:
        raise StartError(f"approved human restart root is unavailable: {error}") from error
    if args.root.resolve(strict=False) != approved_root:
        raise StartError("human restart authority is bound to the exact approved work-log root.")
    if args.human_email_lines != spec.authority_lines:
        raise StartError("human restart authority does not select the exact approved source lines.")
    candidate = args.human_email_file
    if not candidate.is_absolute():
        candidate = args.root / candidate
    try:
        path = candidate.resolve(strict=True)
        approved_path = (approved_root / spec.authority_file).resolve(strict=True)
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
    if digest != spec.authority_sha256:
        raise StartError("human restart authority source content does not match the exact approved email.")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise StartError("human restart authority is not valid UTF-8.") from error
    start_line, end_line = args.human_email_lines
    if end_line > len(lines):
        raise StartError("human restart authority line range exceeds its source email.")
    excerpt = "\n".join(lines[start_line - 1 : end_line])
    if excerpt != spec.authority_text:
        raise StartError("human restart authority excerpt does not match the exact approved request.")
    return path, before, digest


def require_human_restart_authority(args: Args, pane: Pane) -> HumanRestartAuthority | None:
    """Bind the sole approved human-owned respawn to its exact live inputs."""

    if not pane.target.partition(":")[0].startswith("h"):
        return None
    if not args.restart_running:
        raise StartError("human-owned panes support only email-authorized --restart-running recovery.")
    if pane.target == HUMAN_RESTART_TARGET:
        spec = HumanRestartSpec(HUMAN_RESTART_ROOT, HUMAN_RESTART_TASK_FILE, HUMAN_RESTART_AUTHORITY_FILE, HUMAN_RESTART_AUTHORITY_LINES, HUMAN_RESTART_AUTHORITY_SHA256, HUMAN_RESTART_AUTHORITY_TEXT, HUMAN_RESTART_TARGET)
    elif pane.target == HCFG_RESTART_TARGET:
        spec = HumanRestartSpec(HCFG_RESTART_ROOT, HCFG_RESTART_TASK_FILE, HCFG_RESTART_AUTHORITY_FILE, HCFG_RESTART_AUTHORITY_LINES, HCFG_RESTART_AUTHORITY_SHA256, HCFG_RESTART_AUTHORITY_TEXT, HCFG_RESTART_TARGET)
    else:
        raise StartError("human restart authority applies only to the exact approved hwl:3 or hcfg:1 pane.")
    if args.task_file != spec.task_file:
        if spec.target == HUMAN_RESTART_TARGET:
            raise StartError("human restart authority applies only to the exact approved human_task_planner.md task.")
        raise StartError("human restart authority applies only to the exact approved helper_audit_human_facing.md task.")
    path, source, digest = human_restart_source(args, spec)
    return HumanRestartAuthority(
        path,
        spec.authority_lines,
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


def require_source1206_authority(args: Args, pane: Pane) -> Source1206Authority | None:
    """Bind the one approved status-and-stop rotation to its private source."""

    if not args.stop_unverified_replacement:
        return None
    if args.root != SOURCE1206_ROOT:
        raise StartError("the Source-1206 exception applies only to the approved work-log root.")
    if (args.task_file, pane.target) != (SOURCE1206_TASK_FILE, SOURCE1206_TARGET):
        raise StartError("the Source-1206 status-and-stop exception applies only to the approved task and target.")
    if args.audit_output != SOURCE1206_AUDIT_PATH:
        raise StartError("the Source-1206 exception requires its exact one-use audit path.")
    if args.human_email_file is None or args.human_email_lines != SOURCE1206_AUTHORITY_LINES:
        raise StartError("the Source-1206 exception requires its exact human email and line range.")
    candidate = args.human_email_file if args.human_email_file.is_absolute() else args.root / args.human_email_file
    try:
        path = candidate.resolve(strict=True)
        approved = (args.root / SOURCE1206_AUTHORITY_FILE).resolve(strict=True)
        parent = path.parent.stat()
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            data = source.read(HUMAN_RESTART_SOURCE_MAX_BYTES + 1)
            after = os.fstat(source.fileno())
        current = path.stat()
    except OSError as error:
        raise StartError(f"Source-1206 authority source is unavailable: {error}") from error
    before_identity = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
    after_identity = after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    current_identity = current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
    if path != approved or before_identity != after_identity or after_identity != current_identity:
        raise StartError("Source-1206 authority source path or identity is not exact.")
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o077
        or len(data) > HUMAN_RESTART_SOURCE_MAX_BYTES
        or hashlib.sha256(data).hexdigest() != SOURCE1206_AUTHORITY_SHA256
    ):
        raise StartError("Source-1206 authority is not the exact bounded owner-private email.")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise StartError("Source-1206 authority is not valid UTF-8.") from error
    if len(lines) < SOURCE1206_AUTHORITY_LINES[1] or lines[2] != SOURCE1206_APPROVAL or lines[10] != SOURCE1206_PROCEDURE or lines[12] != SOURCE1206_SCOPE:
        raise StartError("Source-1206 authority excerpt does not match the approved one-time procedure.")
    return Source1206Authority(path, SOURCE1206_AUTHORITY_LINES, before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, hashlib.sha256(data).hexdigest(), pane.target, pane.pane_id, pane.window_id, pane.pane_pid)


def verify_source1206_authority(args: Args, pane: Pane, expected: Source1206Authority | None) -> None:
    """Recheck the one-time source and incumbent immediately before replacement."""

    if expected is None:
        return
    current = resolve_pane(pane.target)
    actual = require_source1206_authority(args, current)
    if actual != expected:
        raise StartError("Source-1206 authority or approved pane changed before replacement.")


def verify_task_binding(args: Args, pane: Pane, expected: TaskBinding, *, allow_human_pending: bool = False) -> None:
    """Reject task content or pending-queue drift before or after restart."""

    try:
        actual = validate_task(args, pane, allow_human_pending=allow_human_pending)
    except (OSError, UnicodeError, StartError, ValueError) as error:
        raise StartError(f"task or pending queue no longer has its captured binding: {error}") from error
    if actual != expected:
        raise StartError("task or pending queue changed after restart preparation.")


def rotation_owner_path(args: Args, pane: Pane) -> Path:
    """Return the exact task only when it is the sole active target owner."""

    expected = task_path(args.root, args.task_file).resolve()
    try:
        owners = authoritative_active_target_task_paths(args.root, pane.target)
    except (OSError, UnicodeError, ValueError) as error:
        raise StartError(f"could not prove authoritative rotation ownership: {error}") from error
    if owners != (expected,):
        refs = ", ".join(path.relative_to(args.root.resolve()).as_posix() for path in owners) or "none"
        raise StartError(f"worker rotation requires the task to be the sole authoritative active owner of `{pane.target}`: {refs}.")
    return expected


def rotation_snapshot_sha256(args: Args, pane: Pane, task: TaskBinding, old_session_id: str, owner: Path, todo_sha256: str) -> str:
    """Hash every immutable pre-rotation identity and custody assertion."""

    fields = (
        args.task_file,
        task.task_sha256,
        task.status,
        task.managerat,
        *task.pending_task_items,
        pane.target,
        pane.pane_id,
        pane.window_id,
        str(pane.pane_pid),
        pane.command,
        old_session_id,
        *args.protected_targets,
        owner.relative_to(args.root.resolve()).as_posix(),
        str(args.audit_output),
        todo_sha256,
        *((str(args.human_email_file), str(args.human_email_lines)) if args.stop_unverified_replacement else ()),
    )
    return hashlib.sha256("\0".join(fields).encode()).hexdigest()


def capture_rotation_snapshot(args: Args, pane: Pane, task: TaskBinding) -> RotationSnapshot:
    """Capture the old UUID once and bind it to stable pane and custody state."""

    require_restartable_codex(pane)
    old_session_id, _ = query_status_session_id(
        pane.pane_id,
        240,
        min(10.0, args.startup_timeout_s),
        None,
        (pane.target, pane.pane_id),
    )
    if not old_session_id:
        old_session_id = query_exact_status_session_id(pane, 240, min(10.0, args.startup_timeout_s))
    if args.assert_legacy_missing_session_id:
        if old_session_id:
            raise StartError("legacy missing-session assertion is false because the old worker UUID is recoverable; the pane was not replaced.")
    elif not old_session_id:
        raise StartError("could not capture the current worker session id; the pane was not replaced.")
    current = resolve_pane(pane.target)
    if current != pane:
        raise StartError("worker pane identity or process changed during the atomic rotation snapshot; the pane was not replaced.")
    current_task = validate_task(args, current, verify_target=False)
    if current_task != task:
        raise StartError("task, ordered queue, manager, or custody changed during the atomic rotation snapshot; the pane was not replaced.")
    owner = rotation_owner_path(args, current)
    todo_sha256 = hashlib.sha256((args.root / "TODO.md").read_bytes()).hexdigest()
    audit_path = args.audit_output
    if audit_path is None:
        raise StartError("--rotate-worker requires --audit-output.")
    return RotationSnapshot(
        current,
        current_task,
        old_session_id,
        owner,
        args.protected_targets,
        audit_path,
        todo_sha256,
        rotation_snapshot_sha256(args, current, current_task, old_session_id, owner, todo_sha256),
    )


def verify_rotation_snapshot(args: Args, expected: RotationSnapshot, *, replacement: Pane | None = None) -> Pane:
    """Prove snapshot stability and sole ownership without querying the old UUID again."""

    current = resolve_pane(expected.pane.target)
    same_pane = (current.target, current.pane_id, current.window_id) == (
        expected.pane.target,
        expected.pane.pane_id,
        expected.pane.window_id,
    )
    expected_process = expected.pane if replacement is None else replacement
    valid_replacement = replacement is None or (
        (replacement.target, replacement.pane_id, replacement.window_id) == (expected.pane.target, expected.pane.pane_id, expected.pane.window_id)
        and replacement.pane_pid != expected.pane.pane_pid
        and replacement.command in SUPPORTED_CODEX_PROCESS_COMMANDS
    )
    if not same_pane or not valid_replacement or (current.pane_pid, current.command) != (expected_process.pane_pid, expected_process.command):
        phase = "after replacement" if replacement is not None else "before replacement"
        raise StartError(f"worker pane identity or process does not match the atomic rotation snapshot {phase}.")
    current_task = validate_task(args, current, verify_target=False)
    owner = rotation_owner_path(args, current)
    if (
        current_task != expected.task
        or owner != expected.task_path
        or args.protected_targets != expected.protected_targets
        or args.audit_output != expected.audit_path
        or hashlib.sha256((args.root / "TODO.md").read_bytes()).hexdigest() != expected.todo_sha256
        or rotation_snapshot_sha256(args, expected.pane, current_task, expected.old_session_id, owner, expected.todo_sha256) != expected.sha256
    ):
        raise StartError("task, ordered queue, manager, target, protected set, audit, or sole ownership drifted from the atomic rotation snapshot.")
    return current


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
    if args.model == "gpt-5.6":
        raise StartError("--model gpt-5.6 is not a supported Codex model id; use gpt-5.6-sol, gpt-5.6-terra, or gpt-5.6-luna.")
    executable = CODEX_LAUNCH_COMMAND if tool == "codex" else PCODX_LAUNCH_COMMAND
    codex = [executable]
    if tool == "codex":
        codex.append(CODEX_PACKAGE)
    codex.extend(("--dangerously-bypass-approvals-and-sandbox", "--model", args.model, "--config", f'model_reasoning_effort="{args.reasoning_effort}"'))
    if tool == "codex":
        codex.extend(("--config", "check_for_update_on_startup=false"))
    if tool == "pcodx":
        if pcodx_env is None or tuple(pcodx_env) != PCODX_ENV_KEYS or any(not pcodx_env[key] for key in PCODX_ENV_KEYS):
            raise StartError("PCODX launch requires an exact live state binding.")
    if args.session_id:
        codex.extend(("--cd", str(pane.workdir)))
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


def send_prompt(pane: Pane, prompt_path: Path) -> None:
    """Deliver a fresh-session prompt only after the launch identity is bound."""
    condition = "#{&&:#{==:#{pane_id},%s},#{&&:#{==:#{window_id},%s},#{&&:#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{&&:#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}}}}" % (pane.pane_id, pane.window_id, pane.target, pane.pane_pid, pane.command)
    nonce = f"{os.getpid()}-{time.monotonic_ns()}"
    buffer_name = f"omo-codex-prompt-{nonce}"
    accepted = f"OMO_PROMPT_ACCEPTED_{nonce}"
    rejected = f"OMO_PROMPT_REJECTED_{nonce}"
    loaded = run(["tmux", "set-buffer", "-b", buffer_name, "--", prompt_path.read_text(encoding="utf-8")])
    if loaded.returncode != 0:
        raise StartError("failed to load task prompt; no prompt was sent.")
    sequence = " ; ".join(
        (
            f"paste-buffer -d -b {shlex.quote(buffer_name)} -t {shlex.quote(pane.pane_id)}",
            f"send-keys -t {shlex.quote(pane.pane_id)} Enter",
            f"display-message -p {accepted}",
        )
    )
    try:
        result = run(["tmux", "if-shell", "-F", "-t", pane.target, condition, sequence, f"display-message -p {rejected}"])
    finally:
        _ = run(["tmux", "delete-buffer", "-b", buffer_name])
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise StartError("pane/window/process identity changed before prompt delivery; no prompt was sent.")


def visible_status_card_session_id(text: str) -> str:
    """Return the UUID from the last complete Codex `/status` card only."""
    cards = re.findall(r"╭─+╮\n(?P<body>.*?)\n╰─+╯", text, flags=re.DOTALL)
    for card in reversed(cards):
        if ">_ OpenAI Codex" not in card:
            continue
        matches = re.findall(rf"^│\s*Session:\s*({UUID_RE.pattern[1:-1]})\s*│$", card, flags=re.MULTILINE)
        if len(matches) == 1:
            return matches[0]
    return ""


def exact_response_session_id(text: str) -> tuple[str, str]:
    """Return one response UUID and classify absent or conflicting UUIDs."""

    matches = set(re.findall(rf"Session:\s*({UUID_RE.pattern[1:-1]})", text))
    if not matches:
        return "", "absent"
    if len(matches) != 1:
        return "", "ambiguous"
    return next(iter(matches)), "present"


def query_exact_status_session_id(pane: Pane, n_lines: int, wait_s: float, stale_visible_session_id: str = "", evidence: dict[str, str] | None = None) -> str:
    """Submit `/status` atomically and ignore one UUID found only in prior visible history."""
    condition = "#{&&:#{==:#{pane_id},%s},#{&&:#{==:#{window_id},%s},#{&&:#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{&&:#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}}}}" % (pane.pane_id, pane.window_id, pane.target, pane.pane_pid, pane.command)
    exists, before_lines = exact_tail(pane.target, n_lines)
    if not exists:
        raise StartError("target disappeared before /status query.")
    before = "\n".join(before_lines)
    if stale_visible_session_id and evidence is not None:
        evidence["retained-session-id"] = visible_status_card_session_id(before)
    nonce = f"{os.getpid()}-{time.monotonic_ns()}"
    buffer_name = f"omo-codex-status-{nonce}"
    accepted = f"OMO_STATUS_ACCEPTED_{nonce}"
    loaded = run(["tmux", "set-buffer", "-b", buffer_name, "--", "/status"])
    if loaded.returncode != 0:
        raise StartError("failed to load /status query.")
    sequence = " ; ".join(
        (
            f"paste-buffer -d -b {shlex.quote(buffer_name)} -t {shlex.quote(pane.pane_id)}",
            f"send-keys -t {shlex.quote(pane.pane_id)} Enter",
            f"display-message -p {accepted}",
        )
    )
    try:
        result = run(["tmux", "if-shell", "-F", "-t", pane.target, condition, sequence, "display-message -p OMO_STATUS_REJECTED"])
    finally:
        _ = run(["tmux", "delete-buffer", "-b", buffer_name])
    if result.returncode != 0 or result.stdout != accepted + "\n":
        raise StartError("pane/window/process identity changed before /status submission.")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        verify_same_process(pane)
        exists, after_lines = exact_tail(pane.target, n_lines)
        if not exists:
            raise StartError("target disappeared during /status query.")
        after = "\n".join(after_lines)
        if stale_visible_session_id:
            response = after.rsplit("/status", 1)[-1] if after.count("/status") > before.count("/status") else ""
            session_id, response_state = exact_response_session_id(response)
            if evidence is not None:
                evidence["response-sha256"] = hashlib.sha256(response.encode()).hexdigest()
                evidence["response-session-id"] = session_id
                evidence["response-session-state"] = response_state
        else:
            session_id = extract_new_status_session_id(before, after) or visible_status_card_session_id(after)
        if session_id:
            verify_same_process(pane)
            return session_id
        time.sleep(0.25)
    return ""


def record_session_id(path: Path, session_id: str, expected_sha256: str = "", *, lock_held: bool = False) -> None:
    """Atomically bind the captured Codex UUID in existing task frontmatter."""
    if UUID_RE.fullmatch(session_id) is None:
        raise StartError("Codex status did not return one valid session UUID.")
    before = path.stat()
    text = path.read_text(encoding="utf-8")
    if expected_sha256 and hashlib.sha256(text.encode()).hexdigest() != expected_sha256:
        raise StartError("task changed before session UUID binding; no prompt was sent.")
    parts = frontmatter_parts(text)
    if parts is None:
        raise StartError("task file requires valid frontmatter before session binding.")
    frontmatter, body = parts
    existing = [line.split(":", 1)[1].strip() for line in frontmatter if line.startswith("session_id:")]
    if existing and existing != [session_id]:
        raise StartError("task frontmatter already contains a different Codex session UUID.")
    if not existing:
        frontmatter.append(f"session_id: {session_id}")
    trailing = "\n" if text.endswith("\n") else ""
    try:
        from omo_manager.omo_task import atomic_replace_if_unchanged
    except ModuleNotFoundError:
        from omo_task import atomic_replace_if_unchanged
    try:
        atomic_replace_if_unchanged(path, "\n".join(["---", *frontmatter, "---", *body]) + trailing, before, lock_held=lock_held)
    except (OSError, ValueError) as exc:
        raise StartError(f"task changed during session UUID binding; no prompt was sent: {exc}") from exc


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


def stop_unverified_replacement(pane: Pane, wait_s: float) -> Pane:
    """Atomically replace the exact unverified Codex process with an empty shell."""

    verify_same_process(pane)
    replacement_pids = {pane.pane_pid, *descendant_pids(pane.pane_pid)}
    condition = "#{&&:#{==:#{pane_id},%s},#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        pane.pane_id,
        pane.window_id,
        pane.target,
        pane.pane_pid,
        pane.command,
    )
    stopped = run(
        [
            "tmux",
            "if-shell",
            "-F",
            "-t",
            pane.pane_id,
            condition,
            f"respawn-pane -k -t {shlex.quote(pane.pane_id)} -c {shlex.quote(str(pane.workdir))} /bin/sh",
            "run-shell 'exit 1'",
        ]
    )
    if stopped.returncode != 0:
        raise StartError("failed to stop the exact unverified replacement process.")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        current = resolve_pane(pane.target)
        if current.pane_id != pane.pane_id or current.window_id != pane.window_id:
            raise StartError("pane or window identity changed while stopping the unverified replacement.")
        if current.pane_pid != pane.pane_pid and current.command in SHELL_COMMANDS and all(not Path(f"/proc/{pid}").exists() for pid in replacement_pids):
            return current
        time.sleep(0.25)
    raise StartError("unverified replacement did not stop into an empty shell.")


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
            stream.flush()
            os.fsync(stream.fileno())
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
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid() or stat.S_IMODE(parent_stat.st_mode) & 0o077:
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
            stream.flush()
            os.fsync(stream.fileno())
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


def finish_rotation_audit(
    path: Path,
    prepared: str,
    result: str,
    new_session_id: str = "",
    failure_kind: str = "",
    terminal_owner_task: str = "",
    terminal_replacement: Pane | None = None,
    captured_response_sha256: str = "",
    captured_session_id: str = "",
    stopped_replacement: Pane | None = None,
) -> None:
    """Finalize only the exact owner-private rotation audit reserved by this run."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temporary: Path | None = None
    installed = False
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as output:
            current = os.fstat(output.fileno())
            if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600:
                raise StartError("reserved rotation audit lost its owner-private file binding.")
            if output.read() != prepared:
                raise StartError("reserved rotation audit changed before completion.")
        if terminal_owner_task and terminal_replacement is not None:
            suffix = "\n".join(
                (
                    f"new-session-id: {new_session_id}",
                    f"terminal-replacement-pane-pid: {terminal_replacement.pane_pid}",
                    f"terminal-replacement-command: {terminal_replacement.command}",
                    "terminal-authoritative-owner-count: 1",
                    f"terminal-authoritative-owner-task-file: {terminal_owner_task}",
                    "terminal-prompt-delivery: authorized-after-terminal-sole-owner-proof",
                    "",
                )
            )
        else:
            suffix = f"new-session-id: {new_session_id}\n" if new_session_id else f"failure-kind: {failure_kind}\n" if failure_kind else ""
            if failure_kind:
                response_sha256 = captured_response_sha256 or hashlib.sha256(b"").hexdigest()
                suffix += f"captured-response-sha256: {response_sha256}\n"
            if captured_session_id:
                suffix += f"captured-session-id: {captured_session_id}\n"
            if stopped_replacement is not None:
                suffix += "replacement-disposition: stopped-to-shell\n"
                suffix += f"stopped-pane-pid: {stopped_replacement.pane_pid}\n"
                suffix += f"stopped-command: {stopped_replacement.command}\n"
        finalized = prepared + suffix + f"final-result: {result}\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(finalized)
            output.flush()
            os.fsync(output.fileno())
        latest = path.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            raise StartError("reserved rotation audit changed before atomic finalization.")
        os.replace(temporary, path)
        temporary = None
        installed = True
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if failure_kind == RECONCILABLE_ROTATION_FAILURE_KIND and stopped_replacement is None:
            os.setxattr(path, ROTATION_ELIGIBILITY_XATTR, hashlib.sha256(finalized.encode()).hexdigest().encode(), follow_symlinks=False)
    except OSError as error:
        rollback_error: OSError | None = None
        if installed:
            rollback_temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.rollback.", delete=False) as output:
                    rollback_temporary = Path(output.name)
                    os.fchmod(output.fileno(), 0o600)
                    output.write(prepared)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(rollback_temporary, path)
                rollback_temporary = None
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as caught:
                rollback_error = caught
                try:
                    restored = path.read_text(encoding="utf-8") == prepared
                except (OSError, UnicodeError):
                    restored = False
                if not restored:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as removal_error:
                        rollback_error.add_note(f"audit removal also failed: {removal_error}")
            finally:
                if rollback_temporary is not None:
                    rollback_temporary.unlink(missing_ok=True)
        finalization_error = StartError(f"could not finalize private rotation audit: {error}")
        if rollback_error is not None:
            finalization_error.add_note(f"audit rollback also faulted; the audit cannot claim terminal completion: {rollback_error}")
        raise finalization_error from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def checkpoint_rotation_replacement(path: Path, prepared: str, snapshot: RotationSnapshot, args: Args) -> tuple[str, Pane]:
    """Persist proof that this rotation observed its replacement process."""

    deadline_s = time.monotonic() + min(10.0, args.startup_timeout_s)
    while True:
        current = resolve_pane(snapshot.pane.target)
        if current.pane_id != snapshot.pane.pane_id or current.window_id != snapshot.pane.window_id or current.target != snapshot.pane.target:
            raise StartError("rotation replacement checkpoint found a rebound pane or window.")
        current_task = validate_task(args, current, verify_target=False)
        if current_task != snapshot.task or rotation_owner_path(args, current) != snapshot.task_path:
            raise StartError("rotation replacement checkpoint found task, queue, manager, or sole-owner drift.")
        if current.pane_pid != snapshot.pane.pane_pid and current.command in SUPPORTED_CODEX_PROCESS_COMMANDS:
            current = verify_rotation_snapshot(args, snapshot, replacement=current)
            break
        if time.monotonic() >= deadline_s:
            raise StartError("rotation replacement checkpoint did not observe a new supported Codex process before timeout.")
        time.sleep(0.05)
    checkpointed = prepared + "\n".join(
        (
            "replacement-observed: true",
            f"replacement-target: {current.target}",
            f"replacement-pane-id: {current.pane_id}",
            f"replacement-window-id: {current.window_id}",
            f"replacement-pane-pid: {current.pane_pid}",
            f"replacement-command: {current.command}",
            "",
        )
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    temporary: Path | None = None
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as audit:
            opened = os.fstat(audit.fileno())
            if audit.read() != prepared:
                raise StartError("rotation audit changed before replacement checkpointing.")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as audit:
            temporary = Path(audit.name)
            os.fchmod(audit.fileno(), 0o600)
            audit.write(checkpointed)
            audit.flush()
            os.fsync(audit.fileno())
        latest = path.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise StartError("rotation audit changed before atomic replacement checkpointing.")
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise StartError(f"could not checkpoint rotation replacement: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return checkpointed, current


FAILED_ROTATION_AUDIT_FIELDS = {
    "operation",
    "task-file",
    "target",
    "pane-id",
    "window-id",
    "old-pane-pid",
    "old-command",
    "old-session-id",
    "legacy-missing-session-id",
    "task-sha256",
    "status",
    "blocker-sha256",
    "manager-target",
    "pending-items-sha256",
    "protected-target-count",
    "protected-targets-sha256",
    "authoritative-owner-count",
    "authoritative-owner-task-file",
    "rotation-snapshot-sha256",
    "todo-sha256",
    "prompt-delivery",
    "is-manager",
    "tool",
    "completion",
    "replacement-observed",
    "replacement-target",
    "replacement-pane-id",
    "replacement-window-id",
    "replacement-pane-pid",
    "replacement-command",
    "failure-kind",
    "captured-response-sha256",
    "final-result",
}


def private_regular_file(path: Path, purpose: str) -> tuple[int, os.stat_result]:
    """Open one exact owner-private regular file without following its final path."""

    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise StartError(f"{purpose} parent directory must be owner-private.")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(fd)
    except OSError as error:
        raise StartError(f"could not open exact {purpose}: {error}") from error
    if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid() or stat.S_IMODE(opened.st_mode) != 0o600:
        os.close(fd)
        raise StartError(f"{purpose} must be an owner-private regular file.")
    if purpose == "rotation audit" and opened.st_size > ROTATION_AUDIT_MAX_BYTES:
        os.close(fd)
        raise StartError("rotation audit exceeds the bounded evidence size.")
    return fd, opened


def read_failed_rotation_audit(path: Path, expected_sha256: str) -> RotationAuditBinding:
    fd, before = private_regular_file(path, "rotation audit")
    try:
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            content = stream.read()
            after = os.fstat(stream.fileno())
            try:
                eligibility_commit = os.getxattr(stream.fileno(), ROTATION_ELIGIBILITY_XATTR)
            except OSError:
                eligibility_commit = b""
    except (OSError, UnicodeError) as error:
        raise StartError(f"could not read rotation audit: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
    def identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if identity(before) != identity(after):
        raise StartError("rotation audit changed while it was read.")
    digest = hashlib.sha256(content).hexdigest()
    if SHA256_RE.fullmatch(expected_sha256) is None or digest != expected_sha256:
        raise StartError("rotation audit bytes do not match the expected SHA-256.")
    if eligibility_commit != digest.encode():
        raise StartError("rotation audit lacks the exact committed reconciliation eligibility evidence.")
    if not content.endswith(b"\n") or b"\r" in content:
        raise StartError("rotation audit must use canonical LF-terminated bytes.")
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise StartError(f"rotation audit is not UTF-8: {error}") from error
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(": ")
        if not separator or not key or key in fields:
            raise StartError("rotation audit has invalid or duplicate fields.")
        fields[key] = value
    if set(fields) != FAILED_ROTATION_AUDIT_FIELDS:
        raise StartError("rotation audit does not have the exact failed-rotation schema.")
    if (
        fields["operation"] != "rotate-worker"
        or fields["completion"] != "unknown-until-finalized"
        or fields["failure-kind"] != RECONCILABLE_ROTATION_FAILURE_KIND
        or fields["final-result"] != "failed"
        or SHA256_RE.fullmatch(fields["captured-response-sha256"]) is None
    ):
        raise StartError("rotation audit is not one exact failed rotate-worker record.")
    return RotationAuditBinding(path, before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, content, text, digest, fields)


def reserve_reconciliation_receipt(path: Path, text: str) -> None:
    """Reserve a distinct owner-private reconciliation receipt."""

    try:
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise StartError("reconciliation receipt parent directory must be owner-private.")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
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
        raise StartError(f"could not reserve private reconciliation receipt: {error}") from error


def finish_reconciliation_receipt(path: Path, prepared: str, result: str, current_session_id: str = "") -> None:
    """Atomically finalize only the exact receipt reserved by reconciliation."""

    fd, current = private_regular_file(path, "reconciliation receipt")
    temporary: Path | None = None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as output:
            if output.read() != prepared:
                raise StartError("reserved reconciliation receipt changed before completion.")
        suffix = f"current-session-id: {current_session_id}\n" if current_session_id else ""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(prepared + suffix + f"final-result: {result}\n")
            output.flush()
            os.fsync(output.fileno())
        latest = path.stat()
        if (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            raise StartError("reserved reconciliation receipt changed before atomic finalization.")
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise StartError(f"could not finalize private reconciliation receipt: {error}") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def reconciliation_binding(args: Args) -> ReconciliationBinding:
    audit_path = args.rotation_audit
    if audit_path is None:
        raise StartError("--rotation-audit is required for reconciliation.")
    audit = read_failed_rotation_audit(audit_path, args.expected_rotation_audit_sha256)
    fields = audit.fields
    if args.target.partition(":")[0].startswith("h"):
        raise StartError("rotation audit reconciliation cannot inspect a human-owned `h*` session.")
    if target_is_fresh_rotation_protected(args.target, args.protected_targets):
        raise StartError("reconciliation target is in the explicit protected-target set.")
    pane = resolve_pane(args.target)
    if os.environ.get("TMUX_PANE") == pane.pane_id:
        raise StartError("rotation audit reconciliation cannot query the caller's pane.")
    task = validate_task(args, pane)
    queue_sha256 = hashlib.sha256("\0".join(task.pending_task_items).encode()).hexdigest()
    protected_sha256 = hashlib.sha256("\0".join(args.protected_targets).encode()).hexdigest()
    if task.is_manager or task.tool != "codex" or task.runat != args.target:
        raise StartError("reconciliation requires the exact non-manager Codex task and target.")
    if (task.task_sha256, task.status, task.blocked_on, task.managerat, task.pending_task_items) != (
        args.expected_task_sha256,
        args.expected_status,
        args.expected_blocker,
        args.expected_owner_target,
        args.expected_pending_items,
    ):
        raise StartError("task lifecycle does not match the explicit reconciliation assertions.")
    try:
        old_pid = int(fields["old-pane-pid"])
        replacement_pid = int(fields["replacement-pane-pid"])
        protected_count = int(fields["protected-target-count"])
    except ValueError as error:
        raise StartError("rotation audit has invalid numeric identity fields.") from error
    if (
        fields["task-file"] != args.task_file
        or fields["target"] != pane.target
        or fields["pane-id"] != pane.pane_id
        or fields["window-id"] != pane.window_id
        or fields["task-sha256"] != task.task_sha256
        or fields["status"] != task.status
        or fields["blocker-sha256"] != hashlib.sha256(task.blocked_on.encode()).hexdigest()
        or fields["manager-target"] != task.managerat
        or fields["pending-items-sha256"] != queue_sha256
        or protected_count != len(args.protected_targets)
        or fields["protected-targets-sha256"] != protected_sha256
        or fields["authoritative-owner-count"] != "1"
        or fields["authoritative-owner-task-file"] != args.task_file
        or SHA256_RE.fullmatch(fields["rotation-snapshot-sha256"]) is None
        or fields["prompt-delivery"] != "held-until-terminal-sole-owner-proof"
        or SHA256_RE.fullmatch(fields["todo-sha256"]) is None
        or fields["is-manager"] != "false"
        or fields["tool"] != "codex"
        or fields["legacy-missing-session-id"] != "not-asserted"
        or fields["old-command"] not in SUPPORTED_CODEX_PROCESS_COMMANDS
        or UUID_RE.fullmatch(fields["old-session-id"]) is None
        or fields["replacement-observed"] != "true"
        or fields["replacement-target"] != pane.target
        or fields["replacement-pane-id"] != pane.pane_id
        or fields["replacement-window-id"] != pane.window_id
        or fields["replacement-command"] != pane.command
    ):
        raise StartError("rotation audit does not match the exact task, pane, lifecycle, or protection binding.")
    owner = rotation_owner_path(args, pane)
    snapshot_fields = (
        args.task_file,
        fields["task-sha256"],
        fields["status"],
        fields["manager-target"],
        *args.expected_pending_items,
        fields["target"],
        fields["pane-id"],
        fields["window-id"],
        fields["old-pane-pid"],
        fields["old-command"],
        fields["old-session-id"],
        *args.protected_targets,
        owner.relative_to(args.root.resolve()).as_posix(),
        str(audit.path),
        fields["todo-sha256"],
    )
    if fields["rotation-snapshot-sha256"] != hashlib.sha256("\0".join(snapshot_fields).encode()).hexdigest():
        raise StartError("rotation audit does not match its bound atomic snapshot digest.")
    if owner != task_path(args.root, args.task_file).resolve():
        raise StartError("rotation audit reconciliation requires the same sole authoritative task owner.")
    if pane.pane_pid != args.expected_current_pane_pid or pane.command != args.expected_current_command or pane.command not in SUPPORTED_CODEX_PROCESS_COMMANDS:
        raise StartError("current pane process does not match the explicit reconciliation assertion.")
    if old_pid <= 0 or replacement_pid <= 0 or replacement_pid != pane.pane_pid or pane.pane_pid == old_pid:
        raise StartError("current pane process does not match the failed rotation's observed replacement process.")
    todo = args.root / "TODO.md"
    todo_before = todo.stat()
    todo_bytes = todo.read_bytes()
    todo_after = todo.stat()
    def todo_identity(value: os.stat_result) -> tuple[int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if todo_identity(todo_before) != todo_identity(todo_after):
        raise StartError("TODO changed while its reconciliation binding was read.")
    todo_sha256 = hashlib.sha256(todo_bytes).hexdigest()
    if fields["todo-sha256"] != todo_sha256:
        raise StartError("rotation audit does not match the preserved TODO custody bytes.")
    return ReconciliationBinding(
        audit,
        task,
        pane.target,
        pane.pane_id,
        pane.window_id,
        pane.pane_pid,
        pane.command,
        todo_before.st_dev,
        todo_before.st_ino,
        todo_before.st_size,
        todo_before.st_mtime_ns,
        todo_sha256,
    )


def reconciliation_tmux_condition(pane: Pane | ReconciliationBinding) -> str:
    return "#{&&:#{==:#{pane_id},%s},#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        pane.pane_id,
        pane.window_id,
        pane.target,
        pane.pane_pid,
        pane.command,
    )


def guarded_reconciliation_tmux(pane: Pane | ReconciliationBinding, command: str) -> None:
    result = run(["tmux", "if-shell", "-F", "-t", pane.pane_id, reconciliation_tmux_condition(pane), command, "run-shell 'exit 1'"])
    if result.returncode != 0:
        raise StartError("reconciliation pane/process identity changed before authorized `/status` input.")


def reconciliation_capture(pane: Pane | ReconciliationBinding, n_lines: int) -> str:
    result = run(["tmux", "capture-pane", "-p", "-t", pane.pane_id, "-S", f"-{n_lines}"])
    if result.returncode != 0:
        raise StartError(f"could not capture reconciliation status output: {result.stderr.strip()}")
    return result.stdout


def query_reconciliation_session_id(pane: Pane | ReconciliationBinding, n_lines: int, wait_s: float) -> str:
    """Run the sole authorized `/status` query with a server-side guard per input."""

    before = reconciliation_capture(pane, n_lines)
    buffer_name = f"omo-rotation-reconcile-{os.getpid()}-{time.monotonic_ns()}"
    loaded = run(["tmux", "set-buffer", "-b", buffer_name, "--", "/status"])
    if loaded.returncode != 0:
        raise StartError(f"could not prepare reconciliation status query: {loaded.stderr.strip()}")
    try:
        guarded_reconciliation_tmux(pane, f"paste-buffer -b {shlex.quote(buffer_name)} -t {pane.pane_id}")
    finally:
        _ = run(["tmux", "delete-buffer", "-b", buffer_name])
    guarded_reconciliation_tmux(pane, f"send-keys -t {pane.pane_id} Enter")
    deadline_s = time.monotonic() + wait_s
    fallback_sent = False
    while time.monotonic() < deadline_s:
        after = reconciliation_capture(pane, n_lines)
        session_id = extract_new_status_session_id(before, after)
        if session_id:
            return session_id
        if not fallback_sent and any(line.lstrip().startswith("› ") and "/status" in line for line in after.splitlines()[-20:]):
            guarded_reconciliation_tmux(pane, f"send-keys -t {pane.pane_id} Enter")
            fallback_sent = True
        time.sleep(0.25)
    return ""


def reconcile_rotation_audit_locked(args: Args) -> str:
    """Record later UUID evidence with only one guarded `/status` input query."""

    receipt = args.reconciliation_receipt
    if receipt is None:
        raise StartError("--reconciliation-receipt is required.")
    initial = reconciliation_binding(args)
    if receipt == initial.audit.path:
        raise StartError("reconciliation receipt must be distinct from the original audit.")
    fields = initial.audit.fields
    prepared = "\n".join(
        (
            "operation: reconcile-rotation-audit",
            "evidence-kind: later-reconciliation-only",
            "claim: does-not-rewrite-original-failure-or-explain-immediate-capture-failure",
            f"original-audit-path: {initial.audit.path}",
            f"original-audit-device: {initial.audit.device}",
            f"original-audit-inode: {initial.audit.inode}",
            f"original-audit-size: {initial.audit.size}",
            f"original-audit-mtime-ns: {initial.audit.mtime_ns}",
            f"original-audit-sha256: {initial.audit.sha256}",
            f"original-audit-content-hex: {initial.audit.content.hex()}",
            f"task-file: {args.task_file}",
            f"task-sha256: {initial.task.task_sha256}",
            f"status: {initial.task.status}",
            f"blocker-sha256: {hashlib.sha256(initial.task.blocked_on.encode()).hexdigest()}",
            f"manager-target: {initial.task.managerat}",
            f"pending-items-sha256: {fields['pending-items-sha256']}",
            f"protected-target-count: {fields['protected-target-count']}",
            f"protected-targets-sha256: {fields['protected-targets-sha256']}",
            f"target: {initial.target}",
            f"pane-id: {initial.pane_id}",
            f"window-id: {initial.window_id}",
            f"old-pane-pid: {fields['old-pane-pid']}",
            f"old-command: {fields['old-command']}",
            f"old-session-id: {fields['old-session-id']}",
            f"current-pane-pid: {initial.pane_pid}",
            f"current-command: {initial.command}",
            f"todo-device: {initial.todo_device}",
            f"todo-inode: {initial.todo_inode}",
            f"todo-size: {initial.todo_size}",
            f"todo-mtime-ns: {initial.todo_mtime_ns}",
            f"todo-sha256: {initial.todo_sha256}",
            "is-manager: false",
            "tool: codex",
            "completion: unknown-until-finalized",
            "",
        )
    )
    reserve_reconciliation_receipt(receipt, prepared)
    try:
        if reconciliation_binding(args) != initial:
            raise StartError("audit, task, or pane binding changed after receipt reservation.")
        current_session_id = query_reconciliation_session_id(initial, 240, min(10.0, args.startup_timeout_s))
        if UUID_RE.fullmatch(current_session_id) is None:
            raise StartError("reconciliation status query did not return one valid current Codex UUID.")
        if current_session_id == fields["old-session-id"]:
            raise StartError("reconciliation status query returned the failed rotation's old UUID.")
        if reconciliation_binding(args) != initial:
            raise StartError("audit, task, or pane binding changed during the status query.")
    except Exception as reconciliation_error:
        try:
            finish_reconciliation_receipt(receipt, prepared, "failed")
        except Exception as receipt_error:
            reconciliation_error.add_note(f"receipt finalization also failed; reconciliation remains completion-unknown: {receipt_error}")
        raise
    finish_reconciliation_receipt(receipt, prepared, "success", current_session_id)
    return "rotation-audit-reconciled"


def reconcile_rotation_audit(args: Args) -> str:
    """Hold the task, target, and TODO bindings across reconciliation."""

    task = task_path(args.root, args.task_file)
    todo = args.root / "TODO.md"
    with root_membership_lock(args.root), task_target_lock(args.root, args.target), ExitStack() as locks:
        for path in sorted({task, todo}, key=lambda candidate: str(candidate)):
            locks.enter_context(task_file_lock(path))
        return reconcile_rotation_audit_locked(args)


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


def record_recovery_evidence(
    root: Path,
    pane: Pane,
    output: Path | None,
    delivery_id: str,
    task_file: str = "",
    task: TaskBinding | None = None,
) -> None:
    """Record a helper-produced, one-use receipt after a non-destructive probe."""

    root = root.expanduser().resolve()
    if output is None:
        raise StartError("--record-recovery-evidence requires --recovery-output.")
    if task is None or not task_file:
        raise StartError("recovery evidence requires an exact tracked task and pending-queue binding.")
    if pane.target.partition(":")[0].startswith("h"):
        raise StartError("same-pane recovery evidence cannot be recorded for a human-owned `h*` tmux session.")
    if pane.command in SHELL_COMMANDS or not pane.command:
        raise StartError(f"target {pane.target} is running {pane.command or 'unknown'}, not a verified non-Codex process.")
    if DELIVERY_ID_RE.fullmatch(delivery_id) is None:
        raise StartError("failed delivery id contains unsupported characters.")
    try:
        output_path = output.expanduser().resolve()
        receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
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
    queue_sha256 = hashlib.sha256("\0".join(task.pending_task_items).encode()).hexdigest()
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
        "original_tail_sha256": event_fields["tail_sha256"],
        "tail_sha256": tail_sha256,
        "task_file": task_file,
        "task_sha256": task.task_sha256,
        "task_status": task.status,
        "task_owner": task.managerat,
        "pending_items_sha256": queue_sha256,
    }
    receipt_text = ";".join(f"{key}={value}" for key, value in receipt_fields.items()) + "\n"
    try:
        receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt_stat = receipt_dir.lstat()
    except OSError as error:
        raise StartError(f"recovery evidence directory cannot be prepared: {receipt_dir}: {error}") from error
    if not stat.S_ISDIR(receipt_stat.st_mode) or receipt_stat.st_uid != os.getuid() or stat.S_IMODE(receipt_stat.st_mode) & 0o077:
        raise StartError(f"recovery evidence directory is not a private helper directory: {receipt_dir}")
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


def retire_recovery_receipt(root: Path, evidence: str, *, require_manifest: bool = False) -> None:
    """Remove a consumed recovery transaction after verified replacement startup."""

    root = root.expanduser().resolve()
    receipt = Path(evidence).expanduser().resolve()
    receipt_dir = root / RECOVERY_RECEIPT_DIRNAME
    event_dir = root / DELIVERY_EVENT_DIRNAME
    if receipt.parent != receipt_dir or not receipt.is_relative_to(root):
        raise StartError("recovery evidence receipt must be directly under the helper receipt directory.")
    delivery_id = receipt.name.removesuffix(".receipt")
    if receipt.name != f"{delivery_id}.receipt" or DELIVERY_ID_RE.fullmatch(delivery_id) is None:
        raise StartError("recovery evidence receipt has an invalid event identity.")
    expected_event = delivery_event_path(root, delivery_id).resolve()
    expected_issuance = recovery_issuance_path(receipt)
    expected_paths = (
        expected_event,
        receipt,
        expected_issuance,
        expected_event.with_suffix(".used"),
        receipt.with_name(f"{receipt.name}.used"),
        expected_issuance.with_name(f"{expected_issuance.name}.used"),
    )
    retirement = receipt_dir / f".{delivery_id}.retiring"
    if not any(path.exists() for path in (*expected_paths, retirement)):
        return
    if require_manifest and not retirement.is_file():
        raise StartError("recovery retirement manifest is not available; refusing cleanup-only retirement.")
    if not retirement.exists():
        fields = parse_recovery_fields(recovery_evidence_text(str(receipt)))
        event_path = (root / fields.get("event_file", "")).resolve()
        if event_path != expected_event or not event_path.is_relative_to(event_dir):
            raise StartError("recovery evidence receipt event path is not bound to --root.")
        if not all(path.is_file() for path in expected_paths):
            raise StartError("recovery evidence transaction is incomplete; refusing to retire forensic records.")
        manifest = {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in expected_paths
        }
        write_private_recovery_file_exclusive(retirement, json.dumps(manifest, sort_keys=True) + "\n")
        directory_fd = os.open(receipt_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    try:
        raw_manifest = json.loads(recovery_evidence_text(str(retirement)))
        if not isinstance(raw_manifest, dict) or set(raw_manifest) != {str(path.relative_to(root)) for path in expected_paths}:
            raise StartError("recovery retirement manifest has an invalid record set.")
        for path in expected_paths:
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != raw_manifest[str(path.relative_to(root))]:
                raise StartError(f"recovery record changed during retirement: {path}")
        for path in expected_paths:
            path.unlink(missing_ok=True)
        for directory in (event_dir, receipt_dir):
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        retirement.unlink()
        directory_fd = os.open(receipt_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, json.JSONDecodeError) as error:
        raise StartError(f"verified recovery succeeded but its records could not be fully retired: {error}") from error


def require_recovery_target(
    pane: Pane,
    evidence: str,
    root: Path | None = None,
    task_file: str = "",
    task: TaskBinding | None = None,
) -> None:
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
        "original_tail_sha256",
        "tail_sha256",
        "task_file",
        "task_sha256",
        "task_status",
        "task_owner",
        "pending_items_sha256",
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
    if task is None or not task_file:
        raise StartError("recovery requires an exact tracked task and pending-queue binding.")
    queue_sha256 = hashlib.sha256("\0".join(task.pending_task_items).encode()).hexdigest()
    if (
        fields["task_file"] != task_file
        or fields["task_sha256"] != task.task_sha256
        or fields["task_status"] != task.status
        or fields["task_owner"] != task.managerat
        or fields["pending_items_sha256"] != queue_sha256
    ):
        raise StartError("recovery evidence receipt is not bound to the current task and immutable pending queue.")
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
        "tail_sha256": fields["original_tail_sha256"],
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
    if SHA256_RE.fullmatch(fields["tail_sha256"]) is None or SHA256_RE.fullmatch(fields["original_tail_sha256"]) is None:
        raise StartError("recovery evidence receipt has an invalid status snapshot digest.")
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


def resume_cwd_prompt(lines: list[str]) -> tuple[int, Path, Path] | None:
    """Recognize Codex's exact default resume-directory choice menu."""

    visible = [(index, line.strip()) for index, line in enumerate(lines) if line.strip()]
    n_prompt_lines = len(RESUME_CWD_PROMPT_PREFIX) + len(RESUME_CWD_PROMPT_SUFFIX) + 2
    if len(visible) < n_prompt_lines:
        return None
    prompt = visible[-n_prompt_lines:]
    prompt_lines = tuple(line for _, line in prompt)
    if prompt_lines[:3] != RESUME_CWD_PROMPT_PREFIX or prompt_lines[-3:] != RESUME_CWD_PROMPT_SUFFIX:
        return None
    session_match = RESUME_CWD_SESSION_RE.fullmatch(prompt_lines[3])
    current_match = RESUME_CWD_CURRENT_RE.fullmatch(prompt_lines[4])
    if session_match is None or current_match is None:
        return None
    return prompt[0][0], Path(session_match.group("path")), Path(current_match.group("path"))


def pane_process_argv(pane: Pane) -> tuple[str, ...]:
    try:
        raw = Path(f"/proc/{pane.pane_pid}/cmdline").read_bytes()
    except OSError as error:
        raise StartError("resume-directory recovery could not inspect the pinned pane process; no input was sent.") from error
    if not raw or len(raw) > 64 * 1024 or not raw.endswith(b"\0"):
        raise StartError("resume-directory recovery found an invalid pinned process command line; no input was sent.")
    try:
        return tuple(part.decode("utf-8") for part in raw[:-1].split(b"\0"))
    except UnicodeDecodeError as error:
        raise StartError("resume-directory recovery found a non-UTF-8 pinned process command line; no input was sent.") from error


def require_resume_cwd_process(pane: Pane, session_id: str) -> Pane:
    if pane.target.partition(":")[0].startswith("h"):
        raise StartError("resume-directory recovery cannot modify a human-owned `h*` tmux session.")
    current = resolve_pane(pane.target)
    if current.pane_id != pane.pane_id or current.window_id != pane.window_id:
        raise StartError("tmux pane or window identity changed before resume-directory recovery.")
    if current.pane_pid != pane.pane_pid:
        raise StartError("tmux pane process identity changed before resume-directory recovery.")
    if current.command != CODEX_LAUNCH_COMMAND:
        raise StartError(f"target {current.target} is running {current.command or 'unknown'}, not the Codex launcher.")
    argv = pane_process_argv(current)
    if len(argv) < 2 or Path(argv[0]).name != CODEX_LAUNCH_COMMAND or argv[1] not in SUPPORTED_CODEX_PACKAGES:
        raise StartError(f"target {current.target} process is not the supported Codex package invocation; no input was sent.")
    resumed_sessions = tuple(argv[index + 1] for index, arg in enumerate(argv[:-1]) if arg == "resume")
    if not resumed_sessions or resumed_sessions[-1] != session_id:
        raise StartError(f"target {current.target} process is not resuming the asserted session {session_id}; no input was sent.")
    return current


def require_resume_cwd_prompt(pane: Pane, session_id: str, expected_session_directory: Path) -> None:
    current = require_resume_cwd_process(pane, session_id)
    captured, lines = exact_tail(current.target, 80)
    if not captured:
        raise StartError(f"target {current.target} resume-directory capture failed; no input was sent.")
    require_resume_cwd_process(pane, session_id)
    prompt = resume_cwd_prompt(lines)
    if prompt is None:
        raise StartError(f"target {current.target} does not show the exact Codex resume working-directory menu; no input was sent.")
    _, session_directory, current_directory = prompt
    if session_directory.resolve(strict=False) != expected_session_directory.resolve(strict=False):
        raise StartError(f"target {current.target} does not show the asserted saved session directory; no input was sent.")
    if current_directory.resolve(strict=False) != pane.workdir.resolve(strict=False):
        raise StartError(f"target {current.target} does not show its pinned current working directory; no input was sent.")


def choose_resume_cwd_prompt(pane: Pane, session_id: str, expected_session_directory: Path, choice: str) -> None:
    """Choose `current` or `session` without persisting a Codex preference."""

    require_resume_cwd_prompt(pane, session_id, expected_session_directory)
    key = {"session": "1", "current": "2"}.get(choice)
    if key is None:
        raise StartError("resume-directory recovery choice must be `current` or `session`; no input was sent.")
    condition = "#{&&:#{==:#{window_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        pane.window_id,
        pane.target,
        pane.pane_pid,
        CODEX_LAUNCH_COMMAND,
    )
    send = f"send-keys -t {pane.pane_id} {key} Enter"
    result = run(["tmux", "if-shell", "-F", "-t", pane.pane_id, condition, send, "run-shell 'exit 1'"])
    if result.returncode != 0:
        detail = result.stderr.strip() or "pane/window/process identity changed before resume-directory recovery"
        raise StartError(f"failed to choose the Codex resume directory in {pane.target}: {detail}")
    require_resume_cwd_process(pane, session_id)


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


def wait_resume_cwd_recovery(pane: Pane, session_id: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last_status = ""
    while time.monotonic() < deadline:
        require_resume_cwd_process(pane, session_id)
        report = inspect(StatusArgs(pane.target, 80))
        require_resume_cwd_process(pane, session_id)
        last_status = report.status
        if report.status in SUCCESS_STATUSES:
            return report.status
        if report.status == "error":
            captured, lines = exact_tail(pane.target, 80)
            require_resume_cwd_process(pane, session_id)
            input_text = current_input_text(lines) if captured else ""
            block = current_block(lines) if captured else None
            errors = tuple(visible_error_lines(block.lines or lines[-20:], include_unmarked=True)) if block else ()
            exact_footer = bool(lines and CODEX_FOOTER_RE.match(lines[-1]))
            if captured and errors == RESUME_CWD_IGNORABLE_MCP_ERRORS and resume_cwd_prompt(lines) is None and exact_footer and is_stock_placeholder_input_text(input_text):
                return "ready"
        time.sleep(0.25)
    if last_status == "error":
        raise StartError("Codex resume-directory recovery remained in an error state until timeout.")
    raise StartError("timed out waiting for Codex after choosing its resume directory.")


def wait_started(pane: Pane, marker: str, timeout_s: float, *, allow_update_input: bool = True) -> str:
    deadline = time.monotonic() + timeout_s
    update_skipped = False
    while time.monotonic() < deadline:
        verify_same_pane(pane)
        lines = post_marker_lines(pane, marker)
        if lines is not None and not update_skipped and is_codex_update_prompt(lines):
            if not allow_update_input:
                raise RotationSessionCaptureFailed("Codex startup reached an update prompt before the authorized status query.", "post-respawn-status-query-not-reached")
            skip_codex_update_prompt(pane)
            update_skipped = True
            time.sleep(0.25)
            continue
        classification = "not_codex" if lines is None else classify_status(lines, current_block(lines))
        if classification in SUCCESS_STATUSES:
            return classification
        if classification == "error":
            if not allow_update_input:
                raise RotationSessionCaptureFailed("Codex startup failed before the authorized status query.", "post-respawn-status-query-not-reached")
            raise StartError("Codex startup reached an error state.")
        time.sleep(0.25)
    if not allow_update_input:
        raise RotationSessionCaptureFailed("Codex startup timed out before the authorized status query.", "post-respawn-status-query-not-reached")
    raise StartError("timed out waiting for Codex to become running or ready.")


def verify_restart_continuity(
    args: Args,
    original: Pane,
    session_id: str,
    task: TaskBinding,
    pcodx: Mapping[str, str] | None,
    *,
    allow_human_pending: bool = False,
) -> None:
    """Prove same-pane, resumed-session, task, queue, and PCODX continuity."""

    current = resolve_pane(original.target)
    if current.pane_id != original.pane_id or current.window_id != original.window_id:
        raise StartError("tmux pane or window identity changed after restart.")
    if current.pane_pid == original.pane_pid:
        raise StartError("Codex pane process identity did not change during restart.")
    verify_task_binding(args, current, task, allow_human_pending=allow_human_pending)
    if pcodx is not None and pcodx_state(current) != dict(pcodx):
        raise StartError("live PCODX state did not retain its original session custody after restart.")
    resumed_session, _ = query_status_session_id(current.pane_id, 240, min(10.0, args.startup_timeout_s))
    if resumed_session != session_id:
        raise StartError("restarted Codex did not prove continuity with the captured original session.")


def verify_fresh_rotation(args: Args, snapshot: RotationSnapshot, replacement: Pane, capture_evidence: dict[str, str] | None = None) -> str:
    """Prove same-pane fresh-session startup and unchanged task boundaries."""

    current = verify_rotation_snapshot(args, snapshot, replacement=replacement)
    # 🧑 Source-1206: "after the restart, send only /status ... If the reply is old, missing, or ambiguous, the process will be stopped"
    if args.stop_unverified_replacement:
        fresh_session_id = query_exact_status_session_id(current, 240, min(10.0, args.startup_timeout_s), snapshot.old_session_id, capture_evidence)
    else:
        fresh_session_id, response = query_status_session_id(current.pane_id, 240, min(10.0, args.startup_timeout_s), None, (current.target, current.pane_id), True)
        if capture_evidence is not None:
            capture_evidence["response-sha256"] = hashlib.sha256(response.encode()).hexdigest()
            capture_evidence["response-session-id"] = fresh_session_id
        # 🧑 Source-1183: "Solve this as soon as possible with anything you have"
        if not fresh_session_id:
            fresh_session_id = query_exact_status_session_id(current, 240, min(10.0, args.startup_timeout_s), snapshot.old_session_id, capture_evidence)
    if not fresh_session_id:
        retained_session_id = (capture_evidence or {}).get("retained-session-id", "")
        response_state = (capture_evidence or {}).get("response-session-state", "")
        if response_state == "ambiguous":
            failure_kind = "post-respawn-ambiguous-session-id"
        elif retained_session_id and retained_session_id != snapshot.old_session_id:
            failure_kind = "post-respawn-stale-unrelated-history"
        else:
            failure_kind = RECONCILABLE_ROTATION_FAILURE_KIND
        raise RotationSessionCaptureFailed("rotated worker did not expose a new Codex session id.", failure_kind, (capture_evidence or {}).get("response-sha256", ""), (capture_evidence or {}).get("response-session-id", ""))
    if fresh_session_id == snapshot.old_session_id:
        raise RotationSessionCaptureFailed("rotated worker resumed the old Codex session instead of starting fresh.", "post-respawn-same-old-session-id", (capture_evidence or {}).get("response-sha256", ""), fresh_session_id)
    verify_rotation_snapshot(args, snapshot, replacement=replacement)
    return fresh_session_id


def start(args: Args) -> str:
    if args.reconcile_rotation_audit:
        raise StartError("rotation audit reconciliation must use its isolated reconciliation path.")
    modes = (
        args.restart_running,
        args.rotate_worker,
        args.recover_non_codex,
        args.record_recovery_evidence,
        args.recover_update_prompt,
        args.recover_resume_cwd_prompt,
    )
    if sum(bool(value) for value in modes) > 1:
        raise StartError("launch, restart, rotation, and recovery modes are mutually exclusive.")
    if args.rotate_worker:
        if args.session_id or args.prompt_file is not None:
            raise StartError("--rotate-worker starts fresh from the tracked task and does not accept a session or prompt file.")
        if not all((args.expected_task_sha256, args.expected_status, args.expected_owner_target, args.expected_pending_items, args.protected_targets, args.audit_output)):
            raise StartError("--rotate-worker requires all explicit lifecycle, queue, protection, and audit assertions.")
        if args.target.partition(":")[0].startswith("h"):
            raise StartError("worker rotation cannot modify a human-owned `h*` tmux session.")
        if target_is_fresh_rotation_protected(args.target, args.protected_targets):
            raise StartError("worker rotation target is in the explicit protected-target set.")
        if any(target_identity(target) is None for target in args.protected_targets):
            raise StartError("worker rotation requires exact protected SESSION:WINDOW[.PANE] targets.")
        if args.assert_legacy_missing_session_id != (args.expected_blocker is not None):
            raise StartError("--assert-legacy-missing-session-id and --expected-blocker must be supplied together.")
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
    if args.recover_resume_cwd_prompt and (not args.session_id or args.prompt_file is not None):
        raise StartError("--recover-resume-cwd-prompt requires --session-id and does not accept --prompt-file.")
    if args.recover_resume_cwd_prompt and (not args.resume_cwd_choice or args.expected_session_directory is None):
        raise StartError("--recover-resume-cwd-prompt requires a choice and expected saved session directory.")
    pane = resolve_pane(args.target)
    human_restart_authority = require_human_restart_authority(args, pane)
    source1206_authority = require_source1206_authority(args, pane)
    if os.environ.get("TMUX_PANE") == pane.pane_id:
        raise StartError("run this helper from a different pane than the empty target.")
    if not any(modes):
        require_same_shell(pane)
    path = task_path(args.root, args.task_file)
    membership_lock = root_membership_lock(args.root) if args.rotate_worker else nullcontext()
    with membership_lock, task_target_lock(args.root, pane.target), ExitStack() as lifecycle_locks:
        lock_paths = {path, args.root / "TODO.md"} if args.rotate_worker else {path}
        for lock_path in sorted(lock_paths, key=str):
            lifecycle_locks.enter_context(task_file_lock(lock_path))
        if not args.rotate_worker:
            verify_same_pane(pane)
        if not any(modes):
            require_same_shell(pane)
        task_binding = validate_task(
            args,
            pane,
            verify_target=not args.rotate_worker,
            allow_human_pending=human_restart_authority is not None and human_restart_authority.target == HCFG_RESTART_TARGET,
        )
        if args.restart_running:
            require_restartable_codex(pane)
        if args.recover_non_codex:
            require_recovery_target(pane, args.recovery_evidence, args.root, args.task_file, task_binding)
        if args.record_recovery_evidence:
            if args.dry_run:
                print(f"target: {pane.target}")
                print("mode: record-recovery-evidence")
                print(f"output: {args.recovery_output}")
                return "dry-run"
            record_recovery_evidence(
                args.root,
                pane,
                args.recovery_output,
                args.failed_delivery_id,
                args.task_file,
                task_binding,
            )
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
        if args.recover_resume_cwd_prompt:
            expected_session_directory = args.expected_session_directory
            if expected_session_directory is None:
                raise StartError("--recover-resume-cwd-prompt requires --expected-session-directory.")
            if args.dry_run:
                require_resume_cwd_prompt(pane, args.session_id, expected_session_directory)
                print(f"target: {pane.target}")
                print("mode: recover-resume-cwd-prompt")
                print(f"session: {args.session_id}")
                print(f"choice: {args.resume_cwd_choice}")
                print(f"session-directory: {expected_session_directory}")
                return "dry-run"
            choose_resume_cwd_prompt(pane, args.session_id, expected_session_directory, args.resume_cwd_choice)
            return wait_resume_cwd_recovery(pane, args.session_id, args.startup_timeout_s)
        effective_args = args
        live_pcodx_state: dict[str, str] | None = None
        rotation_snapshot: RotationSnapshot | None = None
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
            rotation_snapshot = capture_rotation_snapshot(args, pane, task_binding)
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
                None if args.rotate_worker or (args.prompt_file is not None and not any((args.restart_running, args.recover_non_codex))) else prompt_path,
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
                    verify_task_binding(args, pane, task_binding)
                    require_recovery_target(pane, args.recovery_evidence, args.root, args.task_file, task_binding)
                    consume_recovery_receipt(args.root, args.recovery_evidence)
                if args.restart_running:
                    verify_task_binding(
                        args,
                        pane,
                        task_binding,
                        allow_human_pending=human_restart_authority is not None and human_restart_authority.target == HCFG_RESTART_TARGET,
                    )
                    if task_binding.tool == "pcodx" and pcodx_state(pane) != live_pcodx_state:
                        raise StartError("live PCODX state changed before respawn; the pane was not replaced.")
                    verify_human_restart_authority(args, pane, human_restart_authority)
                if args.rotate_worker:
                    if rotation_snapshot is None:
                        raise StartError("worker rotation lacks its atomic identity snapshot.")
                    pane = verify_rotation_snapshot(args, rotation_snapshot)
                    audit_path = args.audit_output
                    if audit_path is None:
                        raise StartError("--rotate-worker requires --audit-output.")
                    queue_sha256 = hashlib.sha256("\0".join(task_binding.pending_task_items).encode()).hexdigest()
                    protected_sha256 = hashlib.sha256("\0".join(args.protected_targets).encode()).hexdigest()
                    old_session_evidence = rotation_snapshot.old_session_id or "unavailable-asserted-legacy"
                    prepared_audit = "\n".join(
                        (
                            "operation: rotate-worker",
                            f"task-file: {args.task_file}",
                            f"target: {pane.target}",
                            f"pane-id: {pane.pane_id}",
                            f"window-id: {pane.window_id}",
                            f"old-pane-pid: {pane.pane_pid}",
                            f"old-command: {pane.command}",
                            f"old-session-id: {old_session_evidence}",
                            f"legacy-missing-session-id: {'asserted-and-observed' if args.assert_legacy_missing_session_id else 'not-asserted'}",
                            f"task-sha256: {task_binding.task_sha256}",
                            f"status: {task_binding.status}",
                            f"blocker-sha256: {hashlib.sha256(task_binding.blocked_on.encode()).hexdigest()}",
                            f"manager-target: {task_binding.managerat}",
                            f"pending-items-sha256: {queue_sha256}",
                            f"protected-target-count: {len(args.protected_targets)}",
                            f"protected-targets-sha256: {protected_sha256}",
                            "authoritative-owner-count: 1",
                            f"authoritative-owner-task-file: {args.task_file}",
                            f"rotation-snapshot-sha256: {rotation_snapshot.sha256}",
                            f"todo-sha256: {rotation_snapshot.todo_sha256}",
                            *((f"one-status-authority-sha256: {source1206_authority.source_sha256}", "one-status-procedure: Source-1206") if source1206_authority is not None else ()),
                            "prompt-delivery: held-until-terminal-sole-owner-proof",
                            "is-manager: false",
                            "tool: codex",
                            "completion: unknown-until-finalized",
                            "",
                        )
                    )
                    reserve_rotation_audit(audit_path, prepared_audit)
                    active_audit = prepared_audit
                    capture_evidence: dict[str, str] = {}
                    replacement: Pane | None = None
                    try:
                        pane = verify_rotation_snapshot(args, rotation_snapshot)
                        verify_source1206_authority(args, pane, source1206_authority)
                        respawn_codex(pane, command)
                        active_audit, replacement = checkpoint_rotation_replacement(audit_path, prepared_audit, rotation_snapshot, args)
                        result = wait_started(replacement, marker, args.startup_timeout_s, allow_update_input=False) if args.stop_unverified_replacement else wait_started(replacement, marker, args.startup_timeout_s)
                        new_session_id = verify_fresh_rotation(args, rotation_snapshot, replacement, capture_evidence)
                    except RotationSessionCaptureFailed as rotation_error:
                        stopped_replacement: Pane | None = None
                        if args.stop_unverified_replacement and replacement is not None:
                            try:
                                stopped_replacement = stop_unverified_replacement(replacement, args.startup_timeout_s)
                            except Exception as stop_error:
                                rotation_error.add_note(f"unverified replacement stop or proof failed; audit remains completion-unknown: {stop_error}")
                                raise rotation_error
                        try:
                            finish_rotation_audit(audit_path, active_audit, "failed", failure_kind=rotation_error.failure_kind, captured_response_sha256=rotation_error.response_sha256, captured_session_id=rotation_error.captured_session_id, stopped_replacement=stopped_replacement)
                        except Exception as audit_error:
                            rotation_error.add_note(f"private audit finalization also failed; audit remains completion-unknown: {audit_error}")
                        raise
                    except Exception as rotation_error:
                        try:
                            finish_rotation_audit(audit_path, active_audit, "failed")
                        except Exception as audit_error:
                            rotation_error.add_note(f"private audit finalization also failed; audit remains completion-unknown: {audit_error}")
                        raise
                    try:
                        replacement = verify_rotation_snapshot(args, rotation_snapshot, replacement=replacement)
                    except Exception as rotation_error:
                        try:
                            finish_rotation_audit(audit_path, active_audit, "failed")
                        except Exception as audit_error:
                            rotation_error.add_note(f"private audit finalization also failed; audit remains completion-unknown: {audit_error}")
                        raise
                    finish_rotation_audit(
                        audit_path,
                        active_audit,
                        "success",
                        new_session_id,
                        terminal_owner_task=args.task_file,
                        terminal_replacement=replacement,
                    )
                    replacement = verify_rotation_snapshot(args, rotation_snapshot, replacement=replacement)
                    if prompt_path is None:
                        raise StartError("worker rotation did not prepare its held task prompt.")
                    send_prompt(replacement, prompt_path)
                    return result
                respawn_codex(pane, command)
            else:
                require_same_shell(pane)
                send_shell_command(pane, command)
            result = wait_started(pane, marker, args.startup_timeout_s)
            # A fresh task prompt is intentionally held back until Codex has
            # proved its UUID.  This makes the task binding durable before any
            # user prompt can create work in the session.
            if args.prompt_file is not None and task_binding.tool == "codex" and not any((args.restart_running, args.rotate_worker, args.recover_non_codex)):
                current = resolve_pane(pane.target)
                session_id = query_exact_status_session_id(current, 240, min(10.0, args.startup_timeout_s))
                if args.session_id and session_id != args.session_id:
                    raise StartError("captured Codex session UUID differs from --session-id; no prompt was sent.")
                if not session_id:
                    raise StartError("could not capture the new Codex session id; no prompt was sent.")
                record_session_id(path, session_id, task_binding.task_sha256, lock_held=True)
                if prompt_path is None:
                    raise StartError("task prompt was not prepared; no prompt was sent.")
                send_prompt(current, prompt_path)
            if args.restart_running:
                verify_restart_continuity(
                    effective_args,
                    pane,
                    effective_args.session_id,
                    task_binding,
                    live_pcodx_state,
                    allow_human_pending=human_restart_authority is not None and human_restart_authority.target == HCFG_RESTART_TARGET,
                )
            if args.recover_non_codex:
                retire_recovery_receipt(args.root, args.recovery_evidence)
            return result
        finally:
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.reconcile_rotation_audit:
            result = reconcile_rotation_audit(args)
        elif args.retire_recovery_evidence:
            retire_recovery_receipt(args.root, args.recovery_evidence, require_manifest=True)
            result = "recovery-evidence-retired"
        else:
            result = start(args)
    except (OSError, StartError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"omo_codex_start: {error}", file=sys.stderr)
        return 1
    print(f"omo_codex_start: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
