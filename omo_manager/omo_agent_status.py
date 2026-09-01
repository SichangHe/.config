#!/usr/bin/env python3
"""Print a concise manager agent status summary from markdown and tmux state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_codex_status import Args as StatusArgs
from omo_manager.omo_codex_status import COMPACTING_RE
from omo_manager.omo_codex_status import Report
from omo_manager.omo_codex_status import dismiss_plan_prompt_if_present
from omo_manager.omo_codex_status import exact_pane_id
from omo_manager.omo_codex_status import has_active_plan_prompt
from omo_manager.omo_codex_status import has_selected_model_capacity_warning
from omo_manager.omo_codex_status import ignorable_codex_apps_transport_lines
from omo_manager.omo_codex_status import inspect
from omo_manager.omo_codex_status import interrupt_waiting_subagent_if_present
from omo_manager.omo_codex_status import is_stock_placeholder_input_text
from omo_manager.omo_codex_status import submit_stuck_input_if_present
from omo_manager.omo_codex_status import visible_error_lines
from omo_manager.omo_task_metadata import RETIRED_RUNAT
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_STATUSES  # noqa: F401
from omo_manager.omo_task_metadata import TARGET_RE
from omo_manager.omo_task_metadata import HumanBlocker
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import TaskMetadata
from omo_manager.omo_task_metadata import UniqueKeyLoader
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_task_metadata import parse_task_metadata


def default_state_dir() -> Path:
    env = globals().get("LOCAL_ENV", os.environ)
    return Path(env.get("OMO_MANAGER_STATE_DIR", Path(env.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omo-manager"))


def read_json(path: Path, fallback: dict[str, object]) -> dict[str, object]:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return fallback
    return data if isinstance(data, dict) else fallback


def write_json_private(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != Path("/tmp"):
        path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        _ = handle.write("\n")


def load_local_env() -> dict[str, str]:
    env = dict(os.environ)
    local_env = Path(env.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config" / "omo_manager" / "local.env"))
    if not local_env.is_file():
        return env
    loaded = subprocess.run(["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(local_env)], capture_output=True, timeout=10, check=False)
    if loaded.returncode != 0:
        return env
    for item in loaded.stdout.split(b"\0"):
        if not item or b"=" not in item:
            continue
        raw_key, raw_value = item.split(b"=", 1)
        key = raw_key.decode(errors="ignore")
        if key and key not in os.environ:
            env[key] = raw_value.decode(errors="surrogateescape")
    return env


LOCAL_ENV = load_local_env()
DEFAULT_ROOT = Path(LOCAL_ENV.get("OMO_WORK_LOGS_ROOT", str(Path.home() / "work_logs")))
DEFAULT_REGISTRY = Path(LOCAL_ENV.get("OMO_MANAGER_SESSION_REGISTRY", str(default_state_dir() / "sessions.json")))
DEFAULT_MANAGER_TARGET = ""
MAX_CUSTODY_RECEIPT_BYTES = 1_000_000
PARK_RECEIPT_VERSION = "v1.0.0"
PARK_REATTESTATION_VERSION = "v2.0.0"
PARK_CUSTODY_REATTESTATION_VERSION = "v3.0.0"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
CODEX_SESSION_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
PENDING_TASK_ITEMS_MARKER = "(above are pending task items)"
TASK_RE = re.compile(r"`?([A-Za-z0-9_./-]+\.md)`?")
BLOCKED_DEPENDENCY_LIST_RE = re.compile(r"`?[A-Za-z0-9_./-]+\.md`?(?:\s*,\s*`?[A-Za-z0-9_./-]+\.md`?)*")
CODEX_SESSION_ID_RE = re.compile(r"\b(?:codex\s+session|session_id)\b[^.\n]{0,120}\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
STOPPED_RECORD_RE = re.compile(r"\b(?:preserved|record-only|stopped)\b", re.IGNORECASE)
RESUME_RE = re.compile(r"\bresum(?:e|able|ed|ing)\b", re.IGNORECASE)
CLOSED_CODEX_RECORD_RE = re.compile(r"\bmanager closed Codex agent\b[^\n]*\btmux target\s+`?([^`;\s]+)`?", re.IGNORECASE)
DIRECT_HUMAN_SHUTDOWN_PAUSE_RE = re.compile(
    r"\Apaused by direct human shutdown instruction routed to [A-Za-z0-9_./-]+\.md; non-human pane (?P<target>[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) closed and (?:task record|pending queue) preserved for explicit resume(?:;.*)?\Z",
    re.IGNORECASE,
)
HUMAN_TOKEN_QUOTA_PAUSE_RE = re.compile(
    r"\Ahuman token-quota pause from 202607/manager_mail/85c5dff58359-729\.txt: keep all VL paths closed until explicit resume\Z",
)
TARGET_SESSION_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):")
LOOSE_TARGET_RE = re.compile(r"\b([a-z][A-Za-z0-9_-]*)\s+(\d+)\b")
PORT_RE = re.compile(r"\bport [`']?(\d{2,5})[`']?")
TASK_BATCH_PREFIX_RE = re.compile(r"^(?:active\s+batch|batch|tasks?)\s*:$", re.IGNORECASE)
TASK_CONNECTOR_RE = re.compile(r"^\s*(?:[,/&+]|\band\b|\bor\b)*\s*$")
ARTIFACT_TASK_NAMES = {"ANSWER.md", "PROCESS.md", "TELEMETRY.md"}
ARTIFACT_TASK_DIRS = {"docs", "manager_mail"}
STATUS_DETAIL_RE = re.compile(r"^\((pending|running|long_running|done|blocked)(?::\s*([^)]*))?\)(?:\s+\(([^)]*)\))?$")
RUNAT_RE = re.compile(r"^runat:\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\b")
CLOSE_TARGET_RE = re.compile(r"\btmux target [`']?([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)[`']?")
PERSISTENT_ROLE_RE = re.compile(r"\bpersistent\b.*\brole\b")
HUMAN_WAIT_RE = re.compile(
    r"""
    \Ahuman\Z
    |
    \bwaiting\s+for\s+next\s+human(?!-readable)\b[\w\s`'":;,.()/.-]{0,120}\b(?:review|input|discussion|approval)\b
    |
    \bwaiting\s+(?:on|for)\s+(?:(?:a|the)\s+)?(?:human(?!-readable)|person)(?:'s)?\b(?:\s+(?:action|answers?|approval|authorization|choice|confirmation|decision|discussion|feedback|follow-?up|guidance|input|repl(?:y|ies)|responses?|reviews?)\b|\s+to\b|(?=\s*(?:$|[.;,)])))
    |
    \bwaiting\s+for\s+future\s+human\s+or\s+watcher\s+manager-ops\s+request\b
    |
    \bhuman/helper\b[\w\s`'":;,.()/.-]{0,80}\baudit\b[\w\s`'":;,.()/.-]{0,80}\bdirection\b
    |
    \bhuman[- ]pending\b
    |
    \bdirect\s+human\s+discussion\b
    |
    \bhuman\s+is\s+still\s+talking\b
    |
    \bhuman(?!-readable)-facing\b[\w\s`'":;,.()/.-]{0,120}\b(?:waits?|review|discussion|interactive)\b
    |
    \bhuman\s+direct\b[\w\s`'":;,.()/.-]{0,80}\b(?:review|discussion)\b
    |
    \bhuman\s+interactive\b[\w\s`'":;,.()/.-]{0,80}\b(?:walkthrough|review|discussion)\b
    |
    \bhuman\s+discussion\b
    |
    \bhuman\s+coordination(?:\s+with\s+\S|:\s*\S)
    |
    \bhuman\s+(?:approval|authorization|decision)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)
HUMAN_QUESTION_GATE_RE = re.compile(
    r"(?:\bhuman(?!-readable)\b[^.\n]{0,100}\b(?:answers?|approval|authority|authorization|choice|confirmation|decision|feedback|guidance|input|repl(?:y|ies)|responses?|reviews?|source)\b|\b(?:answers?|approval|authority|authorization|choice|confirmation|decision|feedback|guidance|input|repl(?:y|ies)|responses?|reviews?|source)\b[^.\n]{0,100}\bhuman(?!-readable)\b)",
    re.IGNORECASE,
)
NON_HUMAN_GATE_RE = re.compile(r"\b(?:non[- ]human|human[- ]independent|not\s+human)\b", re.IGNORECASE)
CONCRETE_HUMAN_DECISION_RE = re.compile(r"\Ahuman decision:\s*\S.*\Z", re.IGNORECASE)
ABSENT_BIND_PATH_BLOCKER = "authoritative resolution of world-writable non-sticky /ssd1 absolute-path rebind risk for Docker bind mounts"
ABSENT_BIND_PATH_TASK = "anvl_a59_packet_route_work.md"
ABSENT_BIND_PATH_TARGET = "a52work:2"
ABSENT_BIND_PATH_WORKDIR = Path("/ssd1/sichangheagent/a52work")
BLOCKED_DELIVERY_TASK = "vl_target_select27.md"
BLOCKED_DELIVERY_TARGET = "vl_build_mgr:4"
BLOCKED_DELIVERY_REASON = "final STOP report SHA-256 a187a1c44d639453a5bbd7e8fd9029dd4100dabbf8b7a79464710471748b4f6e has only replay f8f58a97780162ec525554b264ee8240f51ab1e6dfcd5f69177cd8b9e49116be commitment; accepted delivery and exact consumed-closure attestation are absent, so the task contract requires retaining all six items"
BLOCKED_DELIVERY_ITEMS = (
    "Create one independent planning-only standards-selection packet under /ssd1/sichangheagent/vl_artifacts/fresh_target_selection_2026-08-15_g, or preserve terminal STOP.",
    "Read-only bounded freshness scan: inspect manager records/artifacts outside paused DNS fork; identify at least nine plausible standards tracks with evidence of prior use/selection/rejection vs unused; return exact paths and queries/results.",
    "Read-only bounded task: propose exactly three genuinely separate unused standards tracks for a dependency-free Rust library+CLI and bounded Verus core; return authoritative primary URLs, exact standard material, feasibility notes.",
    "Read-only review of /ssd1/sichangheagent/vl_artifacts/fresh_target_selection_2026-08-15_g pre-freeze STOP packet and GATE_STOP_INVENTORY.sha256",
    "Read-only fresh closure audit of /ssd1/sichangheagent/vl_artifacts/fresh_target_selection_2026-08-15_g: verify exact inventory/hashes, authorization byte identity, nine actual query results, no candidates/candidate inventory/frozen verdict, planning-only scope, and modes; return PASS or substantive defects.",
    "Rerun read-only review of corrected /ssd1/sichangheagent/vl_artifacts/fresh_target_selection_2026-08-15_g pre-freeze STOP packet and GATE_STOP_INVENTORY.sha256",
)
VAGUE_STOPPED_HUMAN_WAIT_RE = re.compile(
    r"\A(?:human|human\s+(?:approval|authorization|decision|discussion)|human[- ]pending|direct\s+human\s+discussion|waiting\s+(?:on|for)\s+(?:(?:a|the)\s+)?(?:human|person)(?:'s)?(?:\s+(?:action|answers?|approval|authorization|choice|confirmation|decision|discussion|feedback|follow-?up|guidance|input|repl(?:y|ies)|responses?|reviews?)|\s+to)?)\Z",
    re.IGNORECASE,
)
MANAGER_MD_REREAD_RE = re.compile(r"\b(?:re-?read|read)\b[^.\n?!;]{0,80}\bMANAGER\.md\b|\bMANAGER\.md\b[^.\n?!;]{0,80}\b(?:re-?read|read)\b", re.IGNORECASE)
MANAGER_MD_REREAD_NEGATIVE_RE = re.compile(
    r"\b(?:did not|didn't|not|never|without|failed to|fails to|hasn't|haven't|hadn't|no need to|no need)\b[^.\n?!;]{0,80}\b(?:re-?read|read)\b[^.\n?!;]{0,80}\bMANAGER\.md\b|"
    r"\bMANAGER\.md\b[^.\n?!;]{0,80}\b(?:was|is|has been|will be)?\s*(?:not|never)\b[^.\n?!;]{0,80}\b(?:re-?read|read)\b",
    re.IGNORECASE,
)


def report_output_evidence(report: Report) -> str:
    if not report.lines:
        return ""
    ignored = ignorable_codex_apps_transport_lines(report.lines)
    evidence_lines = [line for index, line in enumerate(report.lines) if index not in ignored]
    if report.status == "running" and is_stock_placeholder_input_text(report.input_text):
        for idx in range(len(evidence_lines) - 1, -1, -1):
            line = evidence_lines[idx].lstrip()
            if line.startswith("›") and is_stock_placeholder_input_text(line[1:].strip()):
                evidence_lines = [candidate for candidate in evidence_lines[:idx] if candidate.strip()]
                break
    tail = evidence_lines[-3:]
    if not tail:
        return ""
    if report.status != "error":
        return " output=" + " / ".join(tail)
    errors = visible_error_lines(report.lines, include_unmarked=False) or visible_error_lines(report.lines)
    if not errors:
        return " output=" + " / ".join(tail)
    evidence = " output=" + " / ".join(errors[-3:])
    if tail != errors[-3:]:
        evidence += " output_tail=" + " / ".join(tail)
    return evidence


def recover_capacity_error(report: Report) -> Report:
    """Recover an exact capacity error hidden by a trailing Codex goal footer."""

    if report.status == "not_codex" and has_selected_model_capacity_warning(report.lines):
        return replace(report, status="error")
    return report


def manager_compaction_needs_reread(report: Report) -> bool:
    if not any(COMPACTING_RE.search(line) is not None for line in report.lines):
        return False
    return not any("?" not in line and MANAGER_MD_REREAD_RE.search(line) is not None and MANAGER_MD_REREAD_NEGATIVE_RE.search(line) is None for line in report.lines)


@dataclass(frozen=True)
class Args:
    root: Path
    registry: Path
    prune_completed: bool
    exit_code_if_active: bool
    problems_only: bool = False
    manager_target: str = ""
    auto_unstick: bool = True


@dataclass(frozen=True)
class TaskLine:
    task_file: str
    section: str
    line: str
    target: str
    port: int | None
    status: str = ""
    persistent_role: bool = False


@dataclass(frozen=True)
class SessionRecord:
    task_file: str
    target: str
    port: int | None
    started_at_s: float


@dataclass(frozen=True)
class StatusRow:
    task_file: str
    status: str
    evidence: str
    persistent_role: bool = False
    task_status: str = ""
    target: str = ""
    unstick: str = ""
    owner_target: str = ""


@dataclass(frozen=True)
class TaskState:
    status: str
    target: str
    port: int | None
    persistent_role: bool = False
    reason: str = ""
    manager_target: str = ""
    is_manager: bool = False
    tool: str = ""


class ParsedArgs(argparse.Namespace):
    root: Path = DEFAULT_ROOT
    registry: Path = DEFAULT_REGISTRY
    prune_completed: bool = False
    exit_code_if_active: bool = False
    problems_only: bool = False
    manager_target: str = DEFAULT_MANAGER_TARGET
    auto_unstick: bool = True


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""The helper reads task links from TODO.md, treats each linked
task's frontmatter as authoritative status, and correlates that state with tmux.
Use --problems-only --no-auto-unstick for a read-only one-shot diagnosis.""",
    )
    _ = parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    _ = parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    _ = parser.add_argument("--prune-completed", action="store_true", help="Remove completed/previous tasks from sessions.json after writing a .bak.TIMESTAMP backup.")
    _ = parser.add_argument("--exit-code-if-active", action="store_true", help="Exit 3 when any task is still active, meaning not done or blocked.")
    _ = parser.add_argument("--problems-only", action="store_true", help="Print only active-agent problems and exit 3 when any are found.")
    _ = parser.add_argument("--manager-target", default=DEFAULT_MANAGER_TARGET, help="Optional manager Codex tmux target to include in problem checks.")
    _ = parser.add_argument("--no-auto-unstick", dest="auto_unstick", action="store_false", help="Report stuck input without sending Enter even when the pane looks safe to submit.")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    return Args(parsed.root.resolve(), parsed.registry, parsed.prune_completed, parsed.exit_code_if_active, parsed.problems_only, parsed.manager_target, parsed.auto_unstick)


def target_aliases(target: str) -> set[str]:
    """Return tmux pane forms that name the same pane with or without `.0`."""
    return {target, target[:-2] if target.endswith(".0") else f"{target}.0"} if target else set()


def same_tmux_target(left: str, right: str) -> bool:
    """Compare tmux targets after accepting the common implicit `.0` pane."""
    return bool(target_aliases(left) & target_aliases(right))


def is_vl_task_file(task_file: str) -> bool:
    name = Path(task_file).name
    return name.startswith("vl_") or "/vl_" in task_file


def task_line_is_vl(task: TaskLine) -> bool:
    return is_vl_task_file(task.task_file) or target_session(task.target) == "vl"


def current_task_paths(root: Path) -> set[Path]:
    return {
        path
        for current_task in parse_task_lines(root / "TODO.md")
        if current_task.section == "todo:current" and (path := resolve_task_path(root, current_task.task_file)) is not None
    }


def is_current_vl_supervisor(task_file: str) -> bool:
    name = Path(task_file).name
    return name.startswith("vl_supervisor_current_") or name.startswith("vl_submanager_current_")


def blocked_dependency_snapshot(root: Path, task: TaskLine, state: TaskState) -> str:
    """Describe an accepted owned dependency tree, or return an empty string."""
    if state.status != "blocked" or not state.is_manager:
        return ""
    task_path = resolve_task_path(root, task.task_file)
    if task_path is None or task_has_pending_marker(task_path):
        return ""
    current_paths = current_task_paths(root)
    if task_path not in current_paths:
        return ""
    seen_paths = {task_path}
    seen_targets: set[str] = set()
    if state.target:
        seen_targets.add(canonical_target(state.target))
    nodes: list[tuple[str, str, str, str, bool, str]] = []

    def add_node(path: Path, node: TaskState) -> None:
        nodes.append(
            (
                str(path.relative_to(root)),
                node.status,
                canonical_target(node.target),
                canonical_target(node.manager_target),
                node.is_manager,
                node.reason,
            )
        )

    def dependencies_are_active(parent_path: Path, parent: TaskState) -> bool:
        add_node(parent_path, parent)
        if BLOCKED_DEPENDENCY_LIST_RE.fullmatch(parent.reason) is None:
            return False
        found = False
        for match in TASK_RE.finditer(parent.reason):
            found = True
            dependency_path = resolve_task_path(root, match.group(1))
            if dependency_path is None or dependency_path in seen_paths:
                return False
            if dependency_path not in current_paths:
                return False
            dependency = scan_task_state(dependency_path, root)
            if dependency is None or task_has_pending_marker(dependency_path):
                return False
            if not parent.target or not dependency.manager_target or not same_tmux_target(dependency.manager_target, parent.target):
                return False
            dependency_target = canonical_target(dependency.target)
            if not dependency_target or dependency_target in seen_targets:
                return False
            seen_paths.add(dependency_path)
            seen_targets.add(dependency_target)
            if dependency.status in {"running", "long_running"}:
                add_node(dependency_path, dependency)
                continue
            if dependency.status != "blocked" or not dependency.is_manager or not dependencies_are_active(dependency_path, dependency):
                return False
        return found

    return json.dumps(nodes, separators=(",", ":")) if dependencies_are_active(task_path, state) else ""


def task_body_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        parts = frontmatter_parts(text)
    except TaskFrontmatterError:
        return ""
    if parts is None:
        return text
    _frontmatter, body = parts
    return "\n".join(body)


def has_stopped_resume_evidence(path: Path) -> bool:
    text = task_body_text(path)
    if not text:
        return False
    return CODEX_SESSION_ID_RE.search(text) is not None and STOPPED_RECORD_RE.search(text) is not None and RESUME_RE.search(text) is not None


def has_closed_codex_evidence(path: Path, target: str) -> bool:
    return any(same_tmux_target(match.group(1), target) for match in CLOSED_CODEX_RECORD_RE.finditer(task_body_text(path)))


def blocked_resumable_dependency_snapshot(root: Path, task: TaskLine, state: TaskState) -> str:
    """Describe an intentionally stopped worker with active blocker records."""

    if state.status != "blocked" or state.is_manager:
        return ""
    task_path = resolve_task_path(root, task.task_file)
    if task_path is None or task_has_pending_marker(task_path):
        return ""
    metadata = read_task_metadata(task_path, root)
    if metadata is None or not metadata.pending_task_items or not has_stopped_resume_evidence(task_path):
        return ""
    current_paths = current_task_paths(root)
    if task_path not in current_paths or BLOCKED_DEPENDENCY_LIST_RE.fullmatch(state.reason) is None:
        return ""
    seen_paths = {task_path}
    seen_targets: set[str] = {canonical_target(state.target)} if state.target else set()
    nodes: list[tuple[str, str, str, str, bool, str]] = [
        (
            str(task_path.relative_to(root)),
            state.status,
            canonical_target(state.target),
            canonical_target(state.manager_target),
            state.is_manager,
            state.reason,
        )
    ]
    found = False
    for match in TASK_RE.finditer(state.reason):
        found = True
        dependency_path = resolve_task_path(root, match.group(1))
        if dependency_path is None or dependency_path in seen_paths or dependency_path not in current_paths:
            return ""
        dependency = scan_task_state(dependency_path, root)
        if dependency is None or dependency.status not in {"running", "long_running"} or task_has_pending_marker(dependency_path):
            return ""
        dependency_target = canonical_target(dependency.target)
        if not dependency_target or dependency_target in seen_targets:
            return ""
        seen_paths.add(dependency_path)
        seen_targets.add(dependency_target)
        nodes.append(
            (
                str(dependency_path.relative_to(root)),
                dependency.status,
                dependency_target,
                canonical_target(dependency.manager_target),
                dependency.is_manager,
                dependency.reason,
            )
        )
    return json.dumps(nodes, separators=(",", ":")) if found else ""


def blocked_status_dependency_snapshot(root: Path, task: TaskLine, state: TaskState) -> str:
    return blocked_dependency_snapshot(root, task, state) or blocked_resumable_dependency_snapshot(root, task, state)


def blocked_dependencies_are_active(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Accept an owned acyclic dependency tree with running report-free leaves."""
    return bool(blocked_dependency_snapshot(root, task, state))


def blocked_resumable_dependencies_are_active(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Accept a stopped worker only when its resume and blocker records are live."""
    return bool(blocked_resumable_dependency_snapshot(root, task, state))


def blocked_closed_manager_dependency_is_active(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Accept a deliberately closed manager with a live named dependency."""
    if state.status not in {"blocked", "long_running"} or not state.is_manager:
        return False
    task_path = resolve_task_path(root, task.task_file)
    if task_path is None or task_has_pending_marker(task_path) or not has_closed_codex_evidence(task_path, state.target):
        return False
    current_paths = current_task_paths(root)
    metadata = read_task_metadata(task_path, root)
    if metadata is None:
        return False
    if metadata.blockers:
        dependency_names = [blocker.task for blocker in metadata.blockers if blocker.kind == "task"]
        if len(dependency_names) != len(metadata.blockers):
            return False
    else:
        dependency_names = [match.group(1) for match in TASK_RE.finditer(state.reason)]
    if not dependency_names or len(set(dependency_names)) != len(dependency_names):
        return False
    seen_targets = {canonical_target(state.target)} if state.target else set()
    dependency_paths: set[Path] = set()
    for dependency_name in dependency_names:
        dependency_path = resolve_task_path(root, dependency_name)
        dependency = scan_task_state(dependency_path, root) if dependency_path is not None else None
        dependency_target = canonical_target(dependency.target) if dependency is not None else ""
        if dependency_path is None or dependency_path == task_path or dependency_path not in current_paths or dependency is None or dependency.status not in {"running", "long_running"} or task_has_pending_marker(dependency_path) or not dependency_target or dependency_target in seen_targets:
            return False
        seen_targets.add(dependency_target)
        dependency_paths.add(dependency_path)
    for current_path in current_paths - {task_path, *dependency_paths}:
        current = scan_task_state(current_path, root)
        if current is not None and canonical_target(current.target) in seen_targets:
            return False
    return True


def quiet_closed_manager_not_codex(root: Path, task: TaskLine, row: StatusRow) -> bool:
    """Return whether a closed manager's empty exact target is intentionally quiet."""
    if row.status not in {"missing", "not_codex"} or " output=" in row.evidence:
        return False
    task_path = resolve_task_path(root, task.task_file)
    state = scan_task_state(task_path, root) if task_path is not None else None
    return state is not None and same_tmux_target(row.target, state.target) and blocked_closed_manager_dependency_is_active(root, task, state)


def is_recorded_human_wait(state: TaskState) -> bool:
    """Return whether a blocked task already records that it is waiting on the human."""

    return HUMAN_WAIT_RE.search(state.reason) is not None and NON_HUMAN_GATE_RE.search(state.reason) is None


def is_authoritative_human_blocked_ready_task(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether authoritative task state makes a ready pane human-blocked."""

    if task.section != "todo:human pending" or state.status != "blocked":
        return False
    task_path = resolve_task_path(root, task.task_file)
    metadata = read_task_metadata(task_path, root)
    indexed = [linked for linked in parse_task_lines(root / "TODO.md") if resolve_task_path(root, linked.task_file) == task_path]
    if (
        task_path is None
        or metadata is None
        or len(indexed) != 1
        or indexed[0].section != "todo:human pending"
        or not indexed[0].target
        or not same_tmux_target(indexed[0].target, state.target)
        or task_has_pending_marker(task_path)
    ):
        return False
    if metadata.blockers:
        if not all(isinstance(blocker, HumanBlocker) for blocker in metadata.blockers):
            return False
        return not any(notice.state in {"pending", "acked"} for item in metadata.pending_items for notice in item.notices)
    exact_human = state.reason.strip().casefold() == "human"
    return (exact_human or HUMAN_QUESTION_GATE_RE.search(state.reason) is not None) and NON_HUMAN_GATE_RE.search(state.reason) is None


def is_historical_human_wait_candidate(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether a sole low-priority worker has a historical human gate."""

    if (
        task.section != "todo:low priority"
        or state.status != "blocked"
        or state.is_manager
        or not state.target
        or not same_tmux_target(task.target, state.target)
    ):
        return False
    task_path = resolve_task_path(root, task.task_file)
    indexed = [linked for linked in parse_task_lines(root / "TODO.md") if resolve_task_path(root, linked.task_file) == task_path]
    if task_path is None or len(indexed) != 1 or task_has_pending_marker(task_path):
        return False
    reason = state.reason.strip()
    if len(reason) >= 2 and reason[0] == reason[-1] and reason[0] in {'"', "'"}:
        reason = reason[1:-1].strip()
    exact_human = reason.lower() == "human"
    protected_concrete_human = (
        target_session(state.target).startswith("h")
        and CONCRETE_HUMAN_DECISION_RE.fullmatch(reason) is not None
        and not pending_task_items(task_path, root)
    )
    return exact_human or protected_concrete_human


def is_intentionally_absent_historical_human_wait(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether a historical human-gated worker's target remains absent."""

    return is_historical_human_wait_candidate(root, task, state) and not target_resolves_exactly(state.target)


def is_bind_path_blocked_worker_candidate(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether this is the exact unsafe bind-path worker record."""
    if (
        task.section not in {"todo:human pending", "todo:low priority"}
        or task.task_file != ABSENT_BIND_PATH_TASK
        or state.status != "blocked"
        or state.reason != ABSENT_BIND_PATH_BLOCKER
        or state.is_manager
        or state.target != ABSENT_BIND_PATH_TARGET
        or not same_tmux_target(task.target, state.target)
    ):
        return False
    task_path = resolve_task_path(root, task.task_file)
    indexed = [linked for linked in parse_task_lines(root / "TODO.md") if resolve_task_path(root, linked.task_file) == task_path]
    if task_path is None or len(indexed) != 1 or task_has_pending_marker(task_path):
        return False
    try:
        parts = frontmatter_parts(task_path.read_text(encoding="utf-8"))
    except (OSError, TaskFrontmatterError):
        return False
    return parts is not None and "No second owner exists." in "\n".join(parts[1])


def is_intentionally_absent_bind_path_blocked_worker(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether the exact unsafe bind-path worker remains deliberately unlaunched."""

    return is_bind_path_blocked_worker_candidate(root, task, state) and not target_resolves_exactly(state.target) and not absent_bind_path_workdir_exists()


def read_digest_bound_file(root: Path, relative: Path, digest: str | None, *, private: bool = False) -> bytes | None:
    """Read one root-confined regular file without following links."""

    if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        directory_fd = os.open(root, directory_flags)
        descriptors.append(directory_fd)
        root_info = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or root_info.st_uid != os.getuid()
            or (private and stat.S_IMODE(root_info.st_mode) & 0o077)
        ):
            return None
        for part in relative.parts[:-1]:
            directory_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
            info = os.fstat(directory_fd)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                return None
        fd = os.open(relative.parts[-1], flags, dir_fd=directory_fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        chunks: list[bytes] = []
        remaining = MAX_CUSTODY_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or (mode != 0o600 if private else bool(mode & 0o022))
        or len(payload) > MAX_CUSTODY_RECEIPT_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (digest is not None and hashlib.sha256(payload).hexdigest() != digest)
    ):
        return None
    return payload


def parse_prior_park_receipt(value: object) -> dict[str, str] | None:
    """Return one exact complete v1 prior-stop receipt."""

    keys = {
        "version", "operation", "task", "target", "pane_id", "task_sha256",
        "initial_todo_sha256", "close_proof_commitment", "prior_close_session_id",
        "authority_source", "authority_sha256", "authority_envelope",
        "authority_envelope_sha256", "state",
    }
    if not isinstance(value, dict) or set(value) != keys or not all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        return None
    record = cast(dict[str, str], value)
    if (
        record["version"] != PARK_RECEIPT_VERSION
        or record["operation"] != "park-unlinked"
        or record["state"] != "complete"
        or record["pane_id"]
        or record["close_proof_commitment"] != "0" * 64
        or CODEX_SESSION_RE.fullmatch(record["prior_close_session_id"]) is None
        or TARGET_RE.fullmatch(record["target"]) is None
        or record["target"].partition(":")[0].startswith("h")
        or any(SHA256_RE.fullmatch(record[key]) is None for key in ("task_sha256", "initial_todo_sha256", "authority_sha256", "authority_envelope_sha256"))
        or re.fullmatch(r"manager_mail/[^/\r\n:]+:[1-9][0-9]*-[1-9][0-9]*", record["authority_source"]) is None
        or re.fullmatch(r"[^/\r\n]+", record["authority_envelope"]) is None
    ):
        return None
    start, end = (int(part) for part in record["authority_source"].rsplit(":", 1)[1].split("-", 1))
    return record if start <= end else None


def parse_prior_park_reattestation(value: object) -> dict[str, object] | None:
    """Return one exact v2 receipt whose embedded v1 receipt authenticates."""

    keys = {
        "version", "operation", "state", "task", "target", "task_sha256", "todo_sha256",
        "authority_source", "authority_sha256", "authority_envelope", "authority_envelope_sha256",
        "prior_complete_receipt_sha256", "prior_complete_receipt",
    }
    if not isinstance(value, dict) or set(value) != keys:
        return None
    scalar_keys = keys - {"prior_complete_receipt"}
    if not all(isinstance(value[key], str) for key in scalar_keys):
        return None
    record = cast(dict[str, object], value)
    prior = parse_prior_park_receipt(record["prior_complete_receipt"])
    if (
        prior is None
        or record["version"] != PARK_REATTESTATION_VERSION
        or record["operation"] != "park-unlinked-re-attestation"
        or record["state"] != "complete"
        or any(SHA256_RE.fullmatch(str(record[key])) is None for key in ("task_sha256", "todo_sha256", "authority_sha256", "authority_envelope_sha256", "prior_complete_receipt_sha256"))
        or record["prior_complete_receipt_sha256"] != hashlib.sha256(yaml.safe_dump(prior, sort_keys=True).encode()).hexdigest()
    ):
        return None
    return record


def parked_custody_sha256(task_ref: str) -> str:
    """Bind stable semantic TODO custody without binding unrelated rows."""

    return hashlib.sha256(f"low priority:\n{task_ref}\n".encode()).hexdigest()


def sole_active_target_owner(root: Path, task_path: Path, target: str) -> bool:
    """Return whether one task is the sole active metadata owner of a target."""

    owners: set[Path] = set()
    for candidate in root.rglob("*.md"):
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            return False
        raw_claim = any(
            key.strip() == "runat" and separator and same_tmux_target(value.strip(), target)
            for key, separator, value in (line.partition(":") for line in text.splitlines())
        )
        try:
            metadata = parse_task_metadata(text, root)
        except TaskFrontmatterError:
            if raw_claim:
                return False
            continue
        if metadata is not None and metadata.status != "done" and same_tmux_target(metadata.runat, target):
            owners.add(candidate.resolve())
    return owners == {task_path.resolve()}


def has_exact_targetless_low_priority_row(root: Path, task_path: Path, task_ref: str, todo_text: str) -> bool:
    """Return whether one TODO snapshot gives the task sole canonical parked custody."""

    section = ""
    low_priority_headers = 0
    references = 0
    canonical_row = False
    known_sections = {"current", "human pending", "low priority", "previous"}
    for line in todo_text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            heading = stripped[:-1]
            section = heading if heading in known_sections and line == f"{heading}:" else ""
            if section == "low priority":
                low_priority_headers += 1
            continue
        if any(resolve_task_path(root, match.group(1)) == task_path for match in TASK_RE.finditer(line)):
            references += 1
            canonical_row = canonical_row or (section == "low priority" and line == task_ref)
    return low_priority_headers == 1 and references == 1 and canonical_row


def has_complete_park_reattestation(root: Path, task_path: Path, state: TaskState, task_text: str) -> bool:
    """Authenticate immutable archived authority for one parked historical target."""

    receipt_path = Path("park-unlinked") / f"{task_path.stem}.yaml"
    receipt_bytes = read_digest_bound_file(default_state_dir(), receipt_path, None, private=True)
    if receipt_bytes is None:
        return False
    try:
        record = yaml.load(receipt_bytes.decode("utf-8"), Loader=UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError, TaskFrontmatterError):
        return False
    keys = {
        "version", "operation", "state", "task", "target", "task_sha256", "custody_sha256",
        "authority_source", "authority_sha256", "authority_envelope", "authority_envelope_sha256",
        "prior_complete_receipt_sha256", "prior_complete_receipt",
    }
    if not isinstance(record, dict) or set(record) != keys:
        return False
    scalar_keys = keys - {"prior_complete_receipt"}
    if not all(isinstance(record[key], str) for key in scalar_keys):
        return False
    prior_reattestation = parse_prior_park_reattestation(record["prior_complete_receipt"])
    prior = parse_prior_park_receipt(prior_reattestation["prior_complete_receipt"]) if prior_reattestation is not None else None
    task_ref = task_path.relative_to(root).as_posix()
    try:
        todo_bytes = (root / "TODO.md").read_bytes()
        todo_text = todo_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    source_match = re.fullmatch(r"([0-9]{4}(?:0[1-9]|1[0-2]))/(manager_mail/[^/\r\n:]+):([1-9][0-9]*-[1-9][0-9]*)", record["authority_source"])
    envelope_match = re.fullmatch(r"([0-9]{4}(?:0[1-9]|1[0-2]))/([^/\r\n]+)", record["authority_envelope"])
    if (
        prior_reattestation is None
        or prior is None
        or record["version"] != PARK_CUSTODY_REATTESTATION_VERSION
        or record["operation"] != "park-unlinked-custody-re-attestation"
        or record["state"] != "complete"
        or record["task"] != task_ref
        or record["target"] != state.target
        or prior_reattestation["task"] != task_ref
        or prior_reattestation["target"] != state.target
        or prior["task"] != task_ref
        or prior["target"] != state.target
        or record["task_sha256"] != hashlib.sha256(task_text.encode()).hexdigest()
        or record["custody_sha256"] != parked_custody_sha256(task_ref)
        or any(SHA256_RE.fullmatch(record[key]) is None for key in ("custody_sha256", "authority_sha256", "authority_envelope_sha256", "prior_complete_receipt_sha256"))
        or record["prior_complete_receipt_sha256"] != hashlib.sha256(yaml.safe_dump(prior_reattestation, sort_keys=True).encode()).hexdigest()
        or record["authority_sha256"] != prior["authority_sha256"]
        or record["authority_source"] != prior_reattestation["authority_source"]
        or record["authority_sha256"] != prior_reattestation["authority_sha256"]
        or record["authority_envelope"] != prior_reattestation["authority_envelope"]
        or record["authority_envelope_sha256"] != prior_reattestation["authority_envelope_sha256"]
        or source_match is None
        or source_match.group(2) != prior["authority_source"].rsplit(":", 1)[0]
        or source_match.group(3) != prior["authority_source"].rsplit(":", 1)[1]
        or envelope_match is None
        or envelope_match.group(1) != source_match.group(1)
        or envelope_match.group(2) != prior["authority_envelope"]
        or len(re.findall(
            rf"^\(manager closed Codex agent [^;\r\n]+; tmux target `{re.escape(state.target)}`; session_id: `{re.escape(prior['prior_close_session_id'])}`\.\)$",
            task_text,
            re.MULTILINE,
        )) != 1
    ):
        return False
    current_locator = str(record["authority_source"])
    prior_locator = prior["authority_source"]
    if current_locator not in task_text or hashlib.sha256(task_text.replace(current_locator, prior_locator).encode()).hexdigest() != prior["task_sha256"]:
        return False
    authority = Path(current_locator.rsplit(":", 1)[0])
    envelope = Path(str(record["authority_envelope"]))
    authority_bytes = read_digest_bound_file(root, authority, str(record["authority_sha256"]))
    envelope_bytes = read_digest_bound_file(root, envelope, str(record["authority_envelope_sha256"]))
    if authority_bytes is None or envelope_bytes is None:
        return False
    try:
        authority_lines = authority_bytes.decode("utf-8").splitlines(keepends=True)
        envelope_text = envelope_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return False
    start, end = (int(part) for part in source_match.group(3).split("-", 1))
    excerpt = "".join(authority_lines[start - 1 : end])
    envelope_pattern = re.compile(
        r'<human_instruction[ \t]+authoritative="true"[ \t]+source="([^"\r\n]+)">\r?\n(.*?)</human_instruction>',
        re.DOTALL,
    )
    matches = [(locator, value.replace("\r\n", "\n")) for locator, value in envelope_pattern.findall(envelope_text)]
    try:
        unchanged = task_path.read_text(encoding="utf-8") == task_text and (root / "TODO.md").read_bytes() == todo_bytes
    except OSError:
        return False
    return (
        1 <= start <= end <= len(authority_lines)
        and matches == [(current_locator, excerpt.replace("\r\n", "\n"))]
        and hashlib.sha256(envelope_text.replace(current_locator, prior_locator).encode()).hexdigest() == prior["authority_envelope_sha256"]
        and has_exact_targetless_low_priority_row(root, task_path, task_ref, todo_text)
        and sole_active_target_owner(root, task_path, state.target)
        and target_resolution_state(state.target) is False
        and unchanged
        and read_digest_bound_file(default_state_dir(), receipt_path, hashlib.sha256(receipt_bytes).hexdigest(), private=True) == receipt_bytes
    )


def is_targetless_low_priority_custody(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether TODO explicitly parks a blocked worker without an owner."""

    if (
        task.section != "todo:low priority"
        or task.line != task.task_file
        or task.target
        or state.status != "blocked"
    ):
        return False
    task_path = resolve_task_path(root, task.task_file)
    try:
        canonical_ref = task_path.relative_to(root).as_posix() if task_path is not None else ""
    except ValueError:
        return False
    if task_path is None or task.task_file != canonical_ref or task_has_pending_marker(task_path):
        return False
    try:
        task_text = task_path.read_text(encoding="utf-8")
    except OSError:
        return False
    metadata = read_task_metadata(task_path, root)
    if (
        state.target
        and metadata is not None
        and metadata.runat == state.target
        and not metadata.is_manager
        and bool(pending_task_items(task_path, root))
        and has_complete_park_reattestation(root, task_path, state, task_text)
    ):
        return True
    genuine_human_gate = NON_HUMAN_GATE_RE.search(state.reason) is None and (
        HUMAN_WAIT_RE.search(state.reason) is not None
        or re.search(r"\b(?:direct human|human halt|human review|human source|human decision|human pending)\b", state.reason, re.IGNORECASE) is not None
    )
    if not genuine_human_gate or state.target:
        return False
    if metadata is None or metadata.runat != "retired":
        return False
    if re.search(r"^\(historical tmux target retired: [A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?; authority: manager_mail/[^\r\n:]+\.txt:\d+-\d+\)$", task_text, re.MULTILINE) is None:
        return False
    try:
        todo_text = (root / "TODO.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return has_exact_targetless_low_priority_row(root, task_path, task.task_file, todo_text)


def absent_bind_path_workdir_exists() -> bool:
    return ABSENT_BIND_PATH_WORKDIR.exists()


def is_blocked_external_delivery_wait(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether the exact completed STOP report awaits external delivery evidence."""

    if (
        task.task_file != BLOCKED_DELIVERY_TASK
        or task.section not in {"todo:human pending", "todo:low priority"}
        or state.status != "blocked"
        or state.reason != BLOCKED_DELIVERY_REASON
        or state.is_manager
        or state.target != BLOCKED_DELIVERY_TARGET
        or not same_tmux_target(task.target, state.target)
    ):
        return False
    task_path = resolve_task_path(root, task.task_file)
    indexed = [linked for linked in parse_task_lines(root / "TODO.md") if resolve_task_path(root, linked.task_file) == task_path]
    return task_path is not None and len(indexed) == 1 and tuple(pending_task_items(task_path, root)) == BLOCKED_DELIVERY_ITEMS and not task_has_pending_marker(task_path)


def task_requires_live_target(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether the task's `runat` represents required live ownership."""

    return not (
        (task.section == "todo:human pending" and not task.target and state.status == "blocked" and is_recorded_human_wait(state))
        or is_intentionally_absent_historical_human_wait(root, task, state)
        or is_intentionally_absent_bind_path_blocked_worker(root, task, state)
        or is_targetless_low_priority_custody(root, task, state)
    )


def task_file_requires_live_target(root: Path, task_file: str) -> bool:
    """Return whether any authoritative TODO link requires the task's live target."""

    path = resolve_task_path(root, task_file)
    tasks = [task for task in parse_task_lines(root / "TODO.md") if resolve_task_path(root, task.task_file) == path]
    state = scan_task_state(path, root) if path is not None else None
    return state is None or not tasks or any(task_requires_live_target(root, task, state) for task in tasks)


def is_explicit_human_pending_wait(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether the task is deliberately closed pending a human decision."""

    if task.section != "todo:human pending" or state.reason.strip().lower() != "human":
        return False
    task_path = resolve_task_path(root, task.task_file)
    return task_path is not None and has_closed_codex_evidence(task_path, state.target)


def is_intentionally_stopped_human_blocked_worker(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether an active worker is deliberately stopped on a human blocker."""

    if (
        task.section not in {"todo:current", "todo:human pending", "todo:low priority"}
        or state.status != "blocked"
        or state.is_manager
        or target_session(state.target).startswith("h")
        or not is_recorded_human_wait(state)
        or VAGUE_STOPPED_HUMAN_WAIT_RE.fullmatch(state.reason.rstrip(" \t.,;:!?")) is not None
        or target_resolves_exactly(state.target)
    ):
        return False
    task_path = resolve_task_path(root, task.task_file)
    return task_path is not None and bool(pending_task_items(task_path, root)) and has_closed_codex_evidence(task_path, state.target)


def is_direct_human_shutdown_pause(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether an exact direct-human shutdown preserves a closed task for resume."""

    match = DIRECT_HUMAN_SHUTDOWN_PAUSE_RE.fullmatch(state.reason)
    if (
        state.status != "blocked"
        or match is None
        or target_session(state.target).startswith("h")
        or not same_tmux_target(match.group("target"), state.target)
        or target_resolves_exactly(state.target)
    ):
        return False
    task_path = resolve_task_path(root, task.task_file)
    return task_path is not None and has_closed_codex_evidence(task_path, state.target)


def is_human_token_quota_pause(root: Path, task: TaskLine, state: TaskState) -> bool:
    """Return whether a source-bound VL quota pause preserves a parked manager."""

    if (
        state.status != "blocked"
        or task.task_file != "vl_build_mgr.md"
        or task.section != "todo:low priority"
        or not state.is_manager
        or HUMAN_TOKEN_QUOTA_PAUSE_RE.fullmatch(state.reason) is None
        or not state.target
        or target_session(state.target).startswith("h")
    ):
        return False
    return resolve_task_path(root, task.task_file) is not None


def canonical_target(target: str) -> str:
    return target[:-2] if target.endswith(".0") else target


def target_session(target: str) -> str:
    match = TARGET_SESSION_RE.match(target)
    return match.group(1) if match is not None else ""


def target_resolves_exactly(target: str) -> bool:
    return bool(exact_pane_id(target))


def target_resolution_state(target: str) -> bool | None:
    """Return present/absent, or None when tmux cannot prove either state."""

    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?", target)
    if match is None:
        return None
    session, window, pane = match.groups()
    try:
        out = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_id}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    found = False
    for line in out.stdout.splitlines():
        fields = line.split("\t")
        if (
            len(fields) != 5
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", fields[0]) is None
            or not fields[1].isdigit()
            or not fields[2].isdigit()
            or fields[3] not in {"0", "1"}
            or re.fullmatch(r"%[0-9]+", fields[4]) is None
        ):
            return None
        row_session, row_window, row_pane, row_active, _row_id = fields
        if row_session == session and row_window == window and (
            (pane is not None and row_pane == pane) or (pane is None and row_active == "1")
        ):
            if found:
                return None
            found = True
    return found


def section_name(line: str, current: str) -> str:
    stripped = line.strip().lower().rstrip(":")
    if stripped in {"current", "previous", "human pending", "low priority"}:
        return stripped
    return current


def task_line_prefix_allows_tasks(prefix: str, first_has_target: bool) -> bool:
    if ";" in prefix:
        prefix = prefix.rsplit(";", 1)[1]
    elif "," in prefix and first_has_target:
        prefix = prefix.rsplit(",", 1)[1]
    stripped = prefix.strip()
    if stripped in {"", "-", "*"}:
        return True
    stripped = re.sub(r"^[-*]\s*", "", stripped)
    return first_has_target and TASK_BATCH_PREFIX_RE.fullmatch(stripped) is not None


def next_task_match_allowed(separator: str, has_target: bool) -> bool:
    cleaned = PORT_RE.sub("", TARGET_RE.sub("", separator))
    if TASK_CONNECTOR_RE.fullmatch(cleaned) is not None:
        return True
    connector_text = re.sub(r"[^A-Za-z]+", "", cleaned).lower()
    if connector_text in {"and", "or"} or (not connector_text and ":" not in cleaned):
        return True
    if re.search(r"[A-Za-z]", cleaned) is not None:
        return False
    if has_target and cleaned.strip() in {"", ",", ";"}:
        return True
    return cleaned.strip() in {"", ",", ";"}


def starts_new_task_entry(separator: str) -> bool:
    return ";" in separator


def semicolon_task_prefix_is_empty(separator: str) -> bool:
    cleaned = PORT_RE.sub("", TARGET_RE.sub("", separator))
    before, after = cleaned.rsplit(";", 1)
    return before.strip() in {"", ",", "and", "or"} and after.strip() in {"", "-", "*"}


def is_artifact_task_ref(task_file: str) -> bool:
    path = Path(task_file)
    return path.name in ARTIFACT_TASK_NAMES or (bool(path.parts) and path.parts[0] in ARTIFACT_TASK_DIRS)


def is_main_manager_task_file(path: Path) -> bool:
    return path.name == "work_manager.md" or path.name.startswith("work_manager_")


def read_task_metadata(path: Path | None, work_log_root: Path | None = None) -> TaskMetadata | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
        return parse_task_metadata(text, work_log_root)
    except (OSError, TaskFrontmatterError):
        return None


def parse_task_lines(path: Path) -> list[TaskLine]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    section = ""
    tasks: list[TaskLine] = []
    for line in lines:
        section = section_name(line, section)
        matches = list(TASK_RE.finditer(line))
        if not matches:
            continue
        line_tasks: list[TaskLine] = []
        previous_match: re.Match[str] | None = None
        for index, match in enumerate(matches):
            task_file = match.group(1)
            if is_artifact_task_ref(task_file):
                previous_match = match
                continue
            next_start = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            segment = line[match.end() : next_start]
            target_match = TARGET_RE.search(segment)
            loose_target_match = LOOSE_TARGET_RE.search(segment) if target_match is None else None
            port_match = PORT_RE.search(segment)
            has_target = target_match is not None or loose_target_match is not None
            if previous_match is None:
                if not task_line_prefix_allows_tasks(line[: match.start()], has_target):
                    continue
            elif not next_task_match_allowed(line[previous_match.end() : match.start()], has_target):
                separator = line[previous_match.end() : match.start()]
                if not starts_new_task_entry(separator) or (not has_target and not semicolon_task_prefix_is_empty(separator)) or not task_line_prefix_allows_tasks(separator, has_target):
                    continue
            target = ""
            if target_match is not None:
                target = target_match.group(1)
            elif loose_target_match is not None:
                target = f"{loose_target_match.group(1)}:{loose_target_match.group(2)}"
            line_tasks.append(
                TaskLine(
                    task_file=task_file,
                    section=f"todo:{section}",
                    line=line.strip(),
                    target=target,
                    port=int(port_match.group(1)) if port_match else None,
                )
            )
            previous_match = match
        tasks.extend(line_tasks)
    return tasks


def resolve_task_path(root: Path, task_file: str) -> Path | None:
    path = Path(task_file).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return None
    try:
        root_resolved = root.resolve(strict=False)
    except OSError:
        root_resolved = root
    if resolved != root_resolved and root_resolved not in resolved.parents:
        return None
    return resolved if resolved.is_file() else None


def managerat_target(path: Path | None, work_log_root: Path | None = None) -> str:
    metadata = read_task_metadata(path, work_log_root)
    return metadata.managerat if metadata is not None else ""


def active_vl_submanager_target(root: Path) -> str:
    """Find the current VL manager pane so VL child tasks route to that owner."""
    for task in parse_task_lines(root / "TODO.md"):
        if task.section != "todo:current" or not is_current_vl_supervisor(task.task_file):
            continue
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path, root) if state_path is not None else None
        if state is not None and state.target:
            return state.target
        if task.target:
            return task.target
    return ""


def effective_owner_target(root: Path, task: TaskLine, state_path: Path | None = None) -> str:
    """Return the manager pane responsible for a task, when ownership is known.

    Frontmatter `managerat` is authoritative. VL child tasks without explicit
    ownership are assigned to the active VL submanager so the top-level manager
    does not report or mutate work owned by that submanager.
    """
    explicit = managerat_target(state_path or resolve_task_path(root, task.task_file), root)
    if explicit:
        return explicit
    if task_line_is_vl(task) and not is_current_vl_supervisor(task.task_file):
        return active_vl_submanager_target(root)
    return ""


def task_owned_by_manager(root: Path, task: TaskLine, manager_target: str, state_path: Path | None = None) -> bool:
    """Return whether `manager_target` should include this task in its view.

    Empty owner or empty manager target means there is no routing filter, so the
    task remains visible to preserve the legacy all-manager view.
    """
    owner = effective_owner_target(root, task, state_path)
    return not owner or not manager_target or same_tmux_target(owner, manager_target)


def owner_target_for_status_row(root: Path, row: StatusRow) -> str:
    """Recover owner routing for rows after classification.

    Problem rows can be built from task files, registry rows, or raw tmux panes.
    This keeps the final output attributable without requiring every caller to
    thread owner metadata through each intermediate row.
    """
    if row.owner_target:
        return row.owner_target
    target = row.target
    if row.task_file.startswith("tmux:"):
        if not target:
            target = row.task_file.removeprefix("tmux:")
        return active_vl_submanager_target(root) if target_session(target) == "vl" else ""
    task_path = resolve_task_path(root, row.task_file)
    todo_tasks = parse_task_lines(root / "TODO.md")
    for task in todo_tasks:
        if task.task_file == row.task_file:
            return effective_owner_target(root, task, task_path)
    fallback = TaskLine(row.task_file, "status-row", "", target, None, row.task_status, row.persistent_role)
    return effective_owner_target(root, fallback, task_path)


def add_owner_to_status_rows(root: Path, rows: list[StatusRow]) -> list[StatusRow]:
    owned: list[StatusRow] = []
    for row in rows:
        owner = owner_target_for_status_row(root, row)
        if not owner:
            owned.append(row)
            continue
        evidence = row.evidence if " owner_target=" in row.evidence else f"{row.evidence} owner_target={owner}"
        owned.append(replace(row, evidence=evidence, owner_target=owner))
    return owned


def scan_legacy_task_state(path: Path) -> TaskState | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    runat_target = ""
    for line in lines:
        runat_match = RUNAT_RE.match(line.strip())
        if runat_match is not None:
            runat_target = runat_match.group(1)
    status = ""
    reason = ""
    target = runat_target
    port: int | None = None
    persistent_role = False
    persistent_role_note_seen = False
    for line in reversed(lines):
        stripped = line.strip()
        if not status:
            status_match = STATUS_DETAIL_RE.match(stripped)
            if status_match is not None:
                status = status_match.group(1)
                reason = (status_match.group(2) or status_match.group(3) or "").strip()
                persistent_role = persistent_role_note_seen or PERSISTENT_ROLE_RE.search(stripped.lower()) is not None
            elif stripped.startswith("(") and stripped.endswith(")") and PERSISTENT_ROLE_RE.search(stripped.lower()) is not None:
                persistent_role_note_seen = True
            else:
                persistent_role_note_seen = False
        if not target:
            close_target_match = CLOSE_TARGET_RE.search(stripped)
            if close_target_match is not None:
                target = close_target_match.group(1)
        if port is None:
            port_match = PORT_RE.search(stripped)
            if port_match is not None:
                port = int(port_match.group(1))
        if status and target and port is not None:
            break
    return TaskState(status, target, port, persistent_role, reason) if status else None


def scan_task_state(path: Path, work_log_root: Path | None = None) -> TaskState | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        metadata = parse_task_metadata(text, work_log_root)
    except TaskFrontmatterError:
        return None
    if metadata is None:
        return scan_legacy_task_state(path) if is_main_manager_task_file(path) else None
    port: int | None = None
    for line in reversed(text.splitlines()):
        port_match = PORT_RE.search(line.strip())
        if port_match is not None:
            port = int(port_match.group(1))
            break
    persistent_role = bool(metadata.blocked_on and PERSISTENT_ROLE_RE.search(metadata.blocked_on.lower()) is not None)
    target = "" if metadata.runat == RETIRED_RUNAT else metadata.runat
    return TaskState(metadata.status, target, port, persistent_role, metadata.blocked_on, metadata.managerat, metadata.is_manager, metadata.tool)


def malformed_active_task_rows(root: Path) -> list[StatusRow]:
    """Report active TODO records that strict task metadata rejects."""
    rows: list[StatusRow] = []
    seen: set[Path] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md":
            continue
        if task.section not in {"todo:current", "todo:human pending", "todo:low priority"}:
            continue
        state_path = resolve_task_path(root, task.task_file)
        if state_path is None or state_path in seen:
            continue
        seen.add(state_path)
        try:
            text = state_path.read_text(encoding="utf-8")
            _ = parse_task_metadata(text, root)
        except OSError:
            continue
        except TaskFrontmatterError as exc:
            reason = " ".join(str(exc).split())
            rows.append(
                StatusRow(
                    task.task_file,
                    "malformed_task",
                    f"strict metadata error: {reason}; repair task frontmatter before relying on lifecycle status",
                    target=task.target,
                )
            )
    return rows


def task_has_pending_marker(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.strip() == "(pending)" for line in lines)


def pending_task_items(path: Path, work_log_root: Path | None = None) -> list[str]:
    metadata = read_task_metadata(path, work_log_root)
    return list(metadata.pending_task_items) if metadata is not None else []


def pending_task_item_rows(root: Path, manager_target: str = "") -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen: set[str] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.task_file in seen:
            continue
        if task.section not in {"todo:current", "todo:human pending", "todo:low priority"}:
            continue
        seen.add(task.task_file)
        state_path = resolve_task_path(root, task.task_file)
        if state_path is None:
            continue
        if not task_owned_by_manager(root, task, manager_target, state_path):
            continue
        state = scan_task_state(state_path, root)
        if state is None or state.status in {"running", "long_running", "pending", "blocked"}:
            continue
        for item in pending_task_items(state_path, root):
            rows.append(StatusRow(task.task_file, "human_request", f"pending_item={item}", task_status=state.status if state is not None else "missing"))
    return rows


def load_task_state(root: Path, manager_target: str = "", *, include_pending_delivery: bool = False) -> tuple[dict[str, TaskLine], set[str], set[str]]:
    todo_tasks = parse_task_lines(root / "TODO.md")
    current: dict[str, TaskLine] = {}
    done: set[str] = set()
    human_pending: set[str] = set()
    for task in todo_tasks:
        if task.task_file == "TODO.md":
            continue
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path, root) if state_path is not None else None
        if state is None:
            continue
        if task_has_pending_marker(state_path) and not (include_pending_delivery and state.status in {"running", "long_running"}):
            continue
        if not task_owned_by_manager(root, task, manager_target, state_path):
            continue
        if state.status == "done":
            done.add(task.task_file)
            continue
        if state.status == "blocked":
            human_pending.add(task.task_file)
            continue
        target = state.target or task.target
        port = state.port if state.port is not None else task.port
        current[task.task_file] = TaskLine(task.task_file, "task-file", task.line, target, port, state.status, state.persistent_role)
    return current, done, human_pending


def persistent_blocked_task_lines(root: Path, manager_target: str = "") -> list[TaskLine]:
    tasks: list[TaskLine] = []
    seen: set[str] = set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.task_file in seen:
            continue
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path, root) if state_path is not None else None
        if state is None or state.status != "blocked" or not state.persistent_role:
            continue
        if not task_owned_by_manager(root, task, manager_target, state_path):
            continue
        target = state.target or task.target
        port = state.port if state.port is not None else task.port
        tasks.append(TaskLine(task.task_file, "task-file", task.line, target, port, state.status, True))
        seen.add(task.task_file)
    return tasks


def current_untracked_task_rows(args: Args, skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for task in parse_task_lines(args.root / "TODO.md"):
        if task.task_file == "TODO.md" or task.section != "todo:current" or not task.target:
            continue
        target = canonical_target(task.target)
        if target in seen_targets:
            continue
        if not task_owned_by_manager(args.root, task, args.manager_target):
            continue
        state_path = resolve_task_path(args.root, task.task_file)
        if task_has_pending_marker(state_path):
            seen_targets.add(target)
            continue
        state = scan_task_state(state_path, args.root) if state_path is not None else None
        if state is not None:
            continue
        row = classify_target(task.task_file, task.target, task_status="unlinked", auto_unstick=auto_unstick, role="todo_current_untracked", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        if row.status in {"error", "ready", "running", "stuck_input"}:
            row = replace(row, status="untracked_agent")
        rows.append(row)
        seen_targets.add(target)
    return rows


def blocked_idle_vl_task_rows(root: Path, manager_target: str = "", auto_unstick: bool = False, unstick_by_target: dict[str, str] | None = None, auto_unstick_disabled_reason: str = "not_problems_only", skip_targets: set[str] | None = None) -> list[StatusRow]:
    todo_tasks = parse_task_lines(root / "TODO.md")
    candidates: list[TaskLine] = []
    for task in todo_tasks:
        if task.task_file == "TODO.md":
            continue
        if task.section == "todo:current" and task_line_is_vl(task):
            candidates.append(task)
        elif is_current_vl_supervisor(task.task_file):
            candidates.append(task)
    rows: list[StatusRow] = []
    seen = {f"target:{canonical_target(target)}" for target in skip_targets or set() if target}
    index = {Path(task.task_file).name: task for task in todo_tasks}
    for task in candidates:
        add_blocked_idle_vl_row(root, task, "blocked_idle_vl", rows, seen, manager_target, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path, root) if state_path is not None else None
        if state is None or state.status != "blocked" or not is_current_vl_supervisor(task.task_file):
            continue
        for match in TASK_RE.finditer(state.reason):
            mentioned = index.get(Path(match.group(1)).name)
            if mentioned is not None:
                add_blocked_idle_vl_row(root, mentioned, "blocked_idle_vl_dependency", rows, seen, manager_target, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
    return rows


def blocked_idle_task_rows(root: Path, manager_target: str = "", auto_unstick: bool = False, unstick_by_target: dict[str, str] | None = None, auto_unstick_disabled_reason: str = "not_problems_only", skip_targets: set[str] | None = None, skip_task_files: set[str] | None = None) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen = {f"target:{canonical_target(target)}" for target in skip_targets or set() if target}
    skip_files = skip_task_files or set()
    for task in parse_task_lines(root / "TODO.md"):
        if task.task_file == "TODO.md" or task.task_file in skip_files:
            continue
        add_blocked_idle_vl_row(root, task, "blocked_idle", rows, seen, manager_target, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
    return rows


def add_blocked_idle_vl_row(root: Path, task: TaskLine, role: str, rows: list[StatusRow], seen: set[str], manager_target: str = "", auto_unstick: bool = False, unstick_by_target: dict[str, str] | None = None, auto_unstick_disabled_reason: str = "not_problems_only") -> None:
    """Report blocked task files whose agent pane is no longer doing work.

    A blocked task is only actionable here when its pane is inspectable and not
    currently running. Errors, missing Codex state, and stuck input keep their
    sharper status; other non-running results become `blocked_idle` with the
    task's blocked reason attached.
    """
    state_path = resolve_task_path(root, task.task_file)
    if task_has_pending_marker(state_path):
        return
    state = scan_task_state(state_path, root) if state_path is not None else None
    if state is None or state.status != "blocked":
        return
    if not task_owned_by_manager(root, task, manager_target, state_path):
        return
    if not task_requires_live_target(root, task, state):
        return
    target = state.target or task.target
    if not target:
        return
    seen_key = f"target:{canonical_target(target)}" if target else f"task:{task.task_file}"
    if seen_key in seen:
        return
    classified: StatusRow | None = None
    idle_status = "blocked_idle"
    quiet_dependency = False
    quiet_resumable = False
    if target:
        classified = classify_target(task.task_file, target, state.persistent_role, state.status, auto_unstick, role, unstick_by_target, auto_unstick_disabled_reason)
        idle_status = classified.status
        if idle_status == "running" and not (is_historical_human_wait_candidate(root, task, state) or is_bind_path_blocked_worker_candidate(root, task, state)):
            return
        if idle_status == "ready" and is_authoritative_human_blocked_ready_task(root, task, state):
            return
        if idle_status == "ready" and is_blocked_external_delivery_wait(root, task, state):
            return
        if is_explicit_human_pending_wait(root, task, state) and idle_status in {"missing", "not_codex"} and " output=" not in classified.evidence:
            return
        if is_intentionally_stopped_human_blocked_worker(root, task, state) and idle_status in {"missing", "not_codex"} and " output=" not in classified.evidence:
            return
        if is_direct_human_shutdown_pause(root, task, state) and idle_status in {"missing", "not_codex"} and " output=" not in classified.evidence:
            return
        if is_human_token_quota_pause(root, task, state) and idle_status in {"missing", "not_codex"}:
            return
        quiet_dependency = blocked_dependencies_are_active(root, task, state) and idle_status == "ready"
        quiet_resumable = blocked_resumable_dependencies_are_active(root, task, state) and idle_status in {"missing", "not_codex"} and " output=" not in classified.evidence and not target_resolves_exactly(target)
        quiet_closed_manager = (
            blocked_closed_manager_dependency_is_active(root, task, state)
            and idle_status in {"missing", "not_codex"}
            and " output=" not in classified.evidence
        )
    reason = state.reason or "blocked with no reason in latest status line"
    evidence = classified.evidence if classified is not None else f"target={target} role={role} task_status=blocked"
    evidence += f" idle_status={idle_status} reason={reason}"
    status = "ready" if quiet_dependency or quiet_resumable or quiet_closed_manager else idle_status if idle_status in {"error", "missing", "not_codex", "stuck_input"} else "blocked_idle"
    rows.append(StatusRow(task.task_file, status, evidence, state.persistent_role, state.status, target, classified.unstick if classified is not None else ""))
    seen.add(seen_key)


def session_records(registry: Path) -> list[SessionRecord]:
    """Load persisted Codex session registry rows.

    Registry `tmux_target` becomes `SessionRecord.target`; this is the code
    path behind the formerly line-numbered status helper question.
    `port` is parsed separately and is only the optional server port.
    """

    raw_obj = read_json(registry, {"sessions": []}).get("sessions", [])
    raw: list[object] = cast(list[object], raw_obj) if isinstance(raw_obj, list) else []
    records: list[SessionRecord] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        typed_item = cast(dict[str, object], item)
        try:
            raw_port = typed_item.get("port")
            port = int(raw_port) if isinstance(raw_port, (int, float, str)) else None
            raw_started = typed_item.get("started_at_s", 0.0)
            started_at_s = float(raw_started) if isinstance(raw_started, (int, float, str)) else 0.0
        except (TypeError, ValueError):
            port = None
            started_at_s = 0.0
        records.append(
            SessionRecord(
                task_file=str(typed_item.get("task_file", "")),
                target=str(typed_item.get("tmux_target", "")),
                port=port,
                started_at_s=started_at_s,
            )
        )
    return [record for record in records if record.task_file]


def choose_session(task: TaskLine, records: list[SessionRecord]) -> SessionRecord | None:
    """Pick the newest registry row that still matches the task target."""

    matches = [record for record in records if record.task_file == task.task_file]
    if not matches:
        return None
    if task.target:
        target_matches = [record for record in matches if same_tmux_target(record.target, task.target)]
        if target_matches:
            matches = target_matches
        else:
            return None
    return max(matches, key=lambda record: record.started_at_s)


def display_target(task: TaskLine, record: SessionRecord | None) -> str:
    """Prefer the live registry tmux target, then fall back to the task line."""

    if record is not None and record.target:
        return record.target
    return task.target


def classify_target(task_file: str, target: str, persistent_role: bool = False, task_status: str = "", auto_unstick: bool = False, role: str = "", unstick_by_target: dict[str, str] | None = None, auto_unstick_disabled_reason: str = "not_problems_only", report: Report | None = None) -> StatusRow:
    """Classify a task by inspecting the Codex state visible in its tmux pane.

    `target` is the only address `inspect` can use. When it is empty, the task
    may still exist in TODO or a task file, but there is no pane to inspect as a
    Codex session. That is reported as `missing` with `target=` evidence so
    callers can surface the broken/missing routing instead of silently dropping
    the task.

    Stuck input is intentionally conservative for blocked tasks, but non-blocked
    panes may receive Enter when `omo_codex_status` says the visible input is
    safe to submit.
    """
    if not target:
        return StatusRow(task_file, "missing", "target=", persistent_role, task_status)
    report = recover_capacity_error(report or inspect(StatusArgs(target, 80)))
    evidence = f"target={target}"
    unstick = ""
    if role:
        evidence += f" role={role}"
    if persistent_role:
        evidence += " persistent_role=true"
    if task_status:
        evidence += f" task_status={task_status}"
    evidence += report_output_evidence(report)
    if report.status == "stuck_input" and persistent_role and task_status == "blocked" and is_stock_placeholder_input_text(report.input_text):
        return StatusRow(task_file, "ready", evidence, persistent_role, task_status, target)
    if report.status == "stuck_input":
        if task_status == "blocked":
            unstick = f"disabled:{role}_blocked" if role else "disabled:blocked"
        elif auto_unstick:
            unstick_key = canonical_target(target)
            if unstick_by_target is not None and unstick_key in unstick_by_target:
                unstick = "already_sent" if unstick_by_target[unstick_key] in {"sent_enter", "sent_escape"} else unstick_by_target[unstick_key]
            else:
                if has_active_plan_prompt(report.lines):
                    recovery = dismiss_plan_prompt_if_present(target, report)
                    evidence += f" recovery={recovery.before}->{recovery.after}"
                    unstick = recovery.action
                else:
                    unstick = submit_stuck_input_if_present(target, report)
                if unstick_by_target is not None:
                    unstick_by_target[unstick_key] = unstick
        else:
            unstick = f"disabled:{auto_unstick_disabled_reason}"
        evidence += f" unstick={unstick}"
    return StatusRow(task_file, report.status, evidence, persistent_role, task_status, target, unstick)


def classify_task(task: TaskLine, record: SessionRecord | None, auto_unstick: bool = False, unstick_by_target: dict[str, str] | None = None, no_auto_unstick_target: str = "", auto_unstick_disabled_reason: str = "not_problems_only") -> StatusRow:
    """Classify a task using its matching registry pane when one exists."""
    target = display_target(task, record)
    return classify_target(task.task_file, target, task.persistent_role, task.status, auto_unstick, unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)


def registry_unmanaged_task(record: SessionRecord, root: Path) -> TaskLine:
    path = resolve_task_path(root, record.task_file)
    state = scan_task_state(path, root) if path is not None else None
    target = record.target or (state.target if state is not None else "")
    port = record.port if record.port is not None or state is None else state.port
    task_status = state.status if state is not None else "unlinked"
    return TaskLine(record.task_file, "registry-unmanaged", "", target, port, task_status, state.persistent_role if state is not None else False)


def registry_unmanaged_problem_rows(
    args: Args,
    records: list[SessionRecord],
    skip_targets: set[str],
    auto_unstick: bool,
    unstick_by_target: dict[str, str],
    auto_unstick_disabled_reason: str,
    active_targets: set[str],
) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    claimed_targets = {canonical_target(target) for target in active_targets if target}
    for record in records:
        target = canonical_target(record.target)
        if not target or target in seen_targets:
            continue
        if not task_file_requires_live_target(args.root, record.task_file):
            continue
        if task_has_pending_marker(resolve_task_path(args.root, record.task_file)):
            continue
        task = registry_unmanaged_task(record, args.root)
        if task.status == "done" and target in claimed_targets:
            continue
        if not task_owned_by_manager(args.root, task, args.manager_target):
            continue
        row = classify_target(task.task_file, task.target, task.persistent_role, task.status, auto_unstick, role="registry_unmanaged", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        problem = unmanaged_problem_row(row, report_not_codex=True, report_ready_running=task.status in {"done", "running"})
        if problem is not None:
            rows.append(problem)
            seen_targets.add(target)
    return rows


def todo_unmanaged_task(root: Path, task: TaskLine) -> TaskLine | None:
    path = resolve_task_path(root, task.task_file)
    state = scan_task_state(path, root) if path is not None else None
    target = task.target
    port = task.port
    task_status = "unlinked"
    persistent_role = False
    if state is not None:
        if not task_requires_live_target(root, task, state):
            return None
        target = target or state.target
        port = port if port is not None else state.port
        task_status = state.status
        persistent_role = state.persistent_role
    if not target:
        return None
    return TaskLine(task.task_file, "todo-unmanaged", task.line, target, port, task_status, persistent_role)


def todo_unmanaged_problem_rows(args: Args, skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for task in parse_task_lines(args.root / "TODO.md"):
        if task_has_pending_marker(resolve_task_path(args.root, task.task_file)):
            continue
        unmanaged = todo_unmanaged_task(args.root, task)
        if unmanaged is None:
            continue
        if not task_owned_by_manager(args.root, unmanaged, args.manager_target):
            continue
        target = canonical_target(unmanaged.target)
        if not target or target in seen_targets:
            continue
        row = classify_target(unmanaged.task_file, unmanaged.target, unmanaged.persistent_role, unmanaged.status, auto_unstick, role="todo_unmanaged", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        problem = unmanaged_problem_row(row, report_not_codex=" output=" in row.evidence, report_ready_running=unmanaged.status == "done")
        if problem is not None:
            rows.append(problem)
            seen_targets.add(target)
    return rows


def owned_todo_targets(args: Args) -> set[str]:
    targets: set[str] = set()
    for task in parse_task_lines(args.root / "TODO.md"):
        unmanaged = todo_unmanaged_task(args.root, task)
        if unmanaged is None:
            continue
        if task_owned_by_manager(args.root, unmanaged, args.manager_target):
            targets.add(unmanaged.target)
    return targets


def active_task_targets(root: Path, *, include_pending_delivery: bool = False) -> set[str]:
    """Return authoritative targets owned by indexed non-completed tasks."""

    targets: set[str] = set()
    for task in parse_task_lines(root / "TODO.md"):
        state_path = resolve_task_path(root, task.task_file)
        state = scan_task_state(state_path, root) if state_path is not None else None
        if state is not None and state.status != "done" and state.target and task_requires_live_target(root, task, state) and (include_pending_delivery or not task_has_pending_marker(state_path)):
            targets.add(state.target)
    return targets


def tmux_list_panes() -> list[str]:
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [canonical_target(line.strip()) for line in out.stdout.splitlines() if line.strip()]


def is_human_tmux_target(target: str) -> bool:
    return target_session(target).startswith("h")


def tmux_unmanaged_problem_rows(args: Args, skip_targets: set[str], auto_unstick: bool, unstick_by_target: dict[str, str], auto_unstick_disabled_reason: str) -> list[StatusRow]:
    rows: list[StatusRow] = []
    seen_targets = {canonical_target(target) for target in skip_targets if target}
    for target in tmux_list_panes():
        if target in seen_targets or is_human_tmux_target(target):
            continue
        if args.manager_target and target_session(target) != target_session(args.manager_target):
            continue
        task_file = f"tmux:{target}"
        if target_session(target) == "vl" and active_vl_submanager_target(args.root) and args.manager_target and target_session(args.manager_target) != "vl":
            continue
        row = classify_target(task_file, target, auto_unstick=auto_unstick, role="tmux_unmanaged", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=auto_unstick_disabled_reason)
        if row.status in {"error", "ready", "running", "stuck_input"}:
            rows.append(replace(row, status="untracked_agent"))
            seen_targets.add(target)
    return rows


def unmanaged_problem_row(row: StatusRow, report_not_codex: bool, report_ready_running: bool) -> StatusRow | None:
    """Keep only unmanaged rows that require manager attention.

    Callers decide whether `not_codex` and `ready` panes are problems because
    registry, TODO, and raw tmux discovery have different confidence levels
    about whether the pane belongs to a managed task.
    """
    if row.status == "ready" and report_ready_running:
        return row
    if row.status in {"error", "stuck_input"}:
        return row
    if row.status in {"missing", "not_codex"} and report_not_codex:
        return row
    return None


def manager_problem_row(args: Args, skip_targets: set[str], unstick_by_target: dict[str, str]) -> StatusRow | None:
    if not args.manager_target:
        return None
    report = recover_capacity_error(inspect(StatusArgs(args.manager_target, 80), detect_waiting_subagent=True))
    evidence = f"target={args.manager_target} role=manager" + report_output_evidence(report)
    if report.status == "waiting_subagent":
        interrupt = interrupt_waiting_subagent_if_present(args.manager_target, report) if args.auto_unstick else "disabled:no_auto_unstick"
        if interrupt == "sent_escape":
            return None
        return StatusRow("manager", "manager_waiting_subagent", f"{evidence} interrupt={interrupt}", target=args.manager_target, unstick=interrupt)
    if manager_compaction_needs_reread(report):
        return StatusRow("manager", "manager_compaction", evidence, target=args.manager_target)
    if any(same_tmux_target(args.manager_target, target) for target in skip_targets):
        return None
    disabled_reason = "no_auto_unstick" if not args.auto_unstick else "not_problems_only"
    row = classify_target("manager", args.manager_target, auto_unstick=args.auto_unstick, role="manager", unstick_by_target=unstick_by_target, auto_unstick_disabled_reason=disabled_reason, report=report)
    return row if row.status in {"error", "missing", "not_codex", "stuck_input"} else None

def registry_prune(args: Args, completed: set[str]) -> int:
    if not completed or not args.registry.exists():
        return 0
    data = read_json(args.registry, {"sessions": []})
    raw_sessions_obj = data.get("sessions", [])
    if not isinstance(raw_sessions_obj, list):
        return 0
    raw_sessions = cast(list[object], raw_sessions_obj)
    kept: list[object] = []
    for item in raw_sessions:
        if isinstance(item, dict):
            typed_item = cast(dict[str, object], item)
            if str(typed_item.get("task_file", "")) in completed:
                continue
        kept.append(cast(object, item))
    removed = len(raw_sessions) - len(kept)
    if removed <= 0:
        return 0
    backup = args.registry.with_name(f"{args.registry.name}.bak")
    _ = shutil.copy2(args.registry, backup)
    data["sessions"] = kept
    write_json_private(args.registry, data)
    return removed


def format_summary(rows: list[StatusRow], completed_stale_count: int, pruned_count: int) -> str:
    rows = [row for row in rows if not is_quiet_blocked_active_row(row)]
    counts: dict[str, int] = {"missing": 0, "not_codex": 0, "running": 0, "blocked_idle": 0, "error": 0, "ready": 0, "stuck_input": 0, "human_request": 0, "malformed_task": 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    lines = [
        f"agent-status: not_codex={counts['not_codex']} running={counts['running']} blocked_idle={counts['blocked_idle']} error={counts['error']} ready={counts['ready']} stuck_input={counts['stuck_input']} human_request={counts['human_request']} malformed_task={counts['malformed_task']} missing={counts['missing']} done-registry-stale={completed_stale_count} pruned={pruned_count}",
    ]
    for row in sorted(rows, key=lambda item: (item.status != "error", item.status, item.task_file)):
        lines.append(f"{row.status}: task={row.task_file} evidence={row.evidence}")
    return "\n".join(lines)


PROBLEM_STATUSES = {"blocked_idle", "error", "human_request", "malformed_task", "manager_compaction", "manager_waiting_subagent", "missing", "not_codex", "ready", "stuck_input", "untracked_agent"}


def is_quiet_blocked_active_row(row: StatusRow) -> bool:
    return row.task_status == "blocked" and row.status in {"ready", "running"}


def is_quiet_long_running_ready_row(row: StatusRow) -> bool:
    return row.task_status == "long_running" and row.status == "ready"


def completed_stale_evidence(root: Path, completed_stale: set[str]) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for task_file in completed_stale:
        row = StatusRow(task_file, "done-stale", "", task_status="done")
        owner = owner_target_for_status_row(root, row)
        owner_suffix = f" owner_target={owner}" if owner else ""
        evidence[task_file] = f"session registry still has a completed task; close it if the pane is still open or prune the registry row{owner_suffix}"
    return evidence


def format_problem_summary(rows: list[StatusRow], completed_stale: set[str] | dict[str, str]) -> str:
    completed_stale_evidence_map = {task_file: "session registry still has a completed task; close it if the pane is still open or prune the registry row" for task_file in completed_stale} if isinstance(completed_stale, set) else completed_stale
    problem_rows = [row for row in rows if row.status in PROBLEM_STATUSES and not is_quiet_blocked_active_row(row)]
    if not problem_rows and not completed_stale_evidence_map:
        return ""
    counts: dict[str, int] = {"missing": 0, "not_codex": 0, "blocked_idle": 0, "error": 0, "human_request": 0, "malformed_task": 0, "manager_compaction": 0, "manager_waiting_subagent": 0, "ready": 0, "stuck_input": 0, "untracked_agent": 0}
    for row in problem_rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    parts = [f"{status}={counts[status]}" for status in ("missing", "not_codex", "blocked_idle", "error", "human_request", "malformed_task", "manager_compaction", "manager_waiting_subagent", "ready", "stuck_input", "untracked_agent") if counts[status]]
    if completed_stale_evidence_map:
        parts.append(f"done-registry-stale={len(completed_stale_evidence_map)}")
    lines = [f"agent-problems: {' '.join(parts)}"]
    if counts["blocked_idle"]:
        lines.append("manager-action: blocked_idle>0 inspect blocked agents, unblock if possible, or route the exact blocker")
    if completed_stale_evidence_map:
        lines.append("manager-action: done-registry-stale>0 close agents marked done but still open, or prune stale registry rows")
    if counts["manager_compaction"]:
        lines.append("manager-action: manager_compaction>0 reread MANAGER.md after compaction unless the compaction summary already included it")
    for row in sorted(problem_rows, key=lambda item: (item.status, item.task_file)):
        lines.append(f"{row.status}: task={row.task_file} evidence={row.evidence} route_owner_target={row.owner_target or '-'}")
    unstuck: dict[str, tuple[str, str]] = {}
    for row in problem_rows:
        if row.unstick in {"sent_enter", "sent_escape"} and row.target:
            unstuck.setdefault(row.target, (row.task_file, row.unstick))
    for target, (task_file, action) in sorted(unstuck.items()):
        lines.append(f"unstuck: target={target} task={task_file} action={action}")
    for task_file, evidence in sorted(completed_stale_evidence_map.items()):
        owner_match = re.search(r"\sowner_target=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)$", evidence)
        owner = owner_match.group(1) if owner_match is not None else "-"
        lines.append(f"done-stale: task={task_file} evidence={evidence} route_owner_target={owner}")
    return "\n".join(lines)


def filter_classified_blocked_ready(root: Path, output: str) -> str:
    """Apply the watcher's exact durable classifications to a standalone scan."""

    from omo_manager.omo_pending_watch import filter_classified_problem_output

    return filter_classified_problem_output(root, output)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        task_args = replace(args, manager_target="")
        current, done, _human_pending = load_task_state(args.root, include_pending_delivery=args.problems_only)
        records = session_records(args.registry)
        tasks = list(current.values())
        if args.problems_only:
            tasks = [task for task in tasks if task.status in {"running", "long_running"}]
        malformed_rows = malformed_active_task_rows(args.root)
        auto_unstick = args.problems_only and args.auto_unstick and not malformed_rows
        auto_unstick_disabled_reason = "malformed_task_present" if malformed_rows else "no_auto_unstick" if args.problems_only and not args.auto_unstick else "not_problems_only"
        unstick_by_target: dict[str, str] = {}
        rows = [classify_task(task, choose_session(task, records), auto_unstick, unstick_by_target, args.manager_target, auto_unstick_disabled_reason) for task in tasks]
        pending_delivery_tasks = {
            task.task_file
            for task in tasks
            if task_has_pending_marker(resolve_task_path(args.root, task.task_file))
        }
        rows = [row for row in rows if row.task_file not in pending_delivery_tasks or row.status == "stuck_input"]
        stuck_pending_delivery_tasks = {row.task_file for row in rows if row.task_file in pending_delivery_tasks}
        inspected_tasks = [task for task in tasks if task.task_file not in pending_delivery_tasks or task.task_file in stuck_pending_delivery_tasks]
        rows.extend(malformed_rows)
        inspected_targets = {display_target(task, choose_session(task, records)) for task in inspected_tasks}
        if args.problems_only:
            manager_row = manager_problem_row(replace(args, auto_unstick=auto_unstick), inspected_targets, unstick_by_target)
            if args.manager_target:
                inspected_targets.add(args.manager_target)
            if manager_row is not None:
                rows.append(manager_row)
                if manager_row.target:
                    inspected_targets.add(manager_row.target)
            untracked_rows = current_untracked_task_rows(task_args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(untracked_rows)
            inspected_targets.update(row.target for row in untracked_rows if row.target)
            rows.extend(pending_task_item_rows(args.root))
            blocked_idle_rows = blocked_idle_vl_task_rows(args.root, "", auto_unstick, unstick_by_target, auto_unstick_disabled_reason, inspected_targets)
            rows.extend(blocked_idle_rows)
            inspected_targets.update(row.target for row in blocked_idle_rows if row.target)
            inspected_task_files = {row.task_file for row in blocked_idle_rows}
            generic_blocked_idle_rows = blocked_idle_task_rows(args.root, "", auto_unstick, unstick_by_target, auto_unstick_disabled_reason, inspected_targets, inspected_task_files)
            rows.extend(generic_blocked_idle_rows)
            inspected_targets.update(row.target for row in generic_blocked_idle_rows if row.target)
            all_active_targets = active_task_targets(args.root, include_pending_delivery=True)
            inspected_targets.update(active_task_targets(args.root))
            inspected_targets.update(display_target(task, choose_session(task, records)) for task in inspected_tasks)
            unmanaged_rows = registry_unmanaged_problem_rows(
                task_args,
                records,
                inspected_targets,
                auto_unstick,
                unstick_by_target,
                auto_unstick_disabled_reason,
                all_active_targets,
            )
            rows.extend(unmanaged_rows)
            inspected_targets.update(record.target for record in records if record.target and task_file_requires_live_target(args.root, record.task_file))
            inspected_targets.update(row.target for row in unmanaged_rows if row.target)
            inspected_targets.update(all_active_targets)
            todo_rows = todo_unmanaged_problem_rows(task_args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(todo_rows)
            inspected_targets.update(owned_todo_targets(task_args))
            inspected_targets.update(row.target for row in todo_rows if row.target)
            tmux_rows = tmux_unmanaged_problem_rows(task_args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(tmux_rows)
            inspected_targets.update(row.target for row in tmux_rows if row.target)
            rows = add_owner_to_status_rows(
                args.root,
                [
                    row
                    for row in rows
                    if not is_quiet_long_running_ready_row(row)
                    and not (
                        (current_task := current.get(row.task_file)) is not None
                        and quiet_closed_manager_not_codex(args.root, current_task, row)
                    )
                ],
            )
        else:
            untracked_rows = current_untracked_task_rows(task_args, inspected_targets, auto_unstick, unstick_by_target, auto_unstick_disabled_reason)
            rows.extend(untracked_rows)
            inspected_targets.update(row.target for row in untracked_rows if row.target)
            rows.extend(blocked_idle_vl_task_rows(args.root, ""))
        completed_stale = {record.task_file for record in records if record.task_file in done}
        pruned_count = registry_prune(args, completed_stale) if args.prune_completed else 0
        if args.problems_only:
            text = format_problem_summary(rows, completed_stale_evidence(args.root, completed_stale))
            text = filter_classified_blocked_ready(args.root, text)
            if not text:
                return 0
            print(text)
            return 3
        print(format_summary(rows, len(completed_stale), pruned_count))
        active_rows = [row for row in rows if not is_quiet_blocked_active_row(row)]
        if args.exit_code_if_active and any(row.status != "blocked_idle" for row in active_rows):
            return 3
    except Exception as exc:
        print(f"omo_agent_status: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
