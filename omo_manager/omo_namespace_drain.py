#!/usr/bin/env python3
"""Plan and execute a reviewed, no-mail task drain for named namespaces."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import re
import select
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import ScalarToken

from omo_manager.omo_task_metadata import TaskFrontmatterError, UniqueKeyLoader, frontmatter_text, parse_task_metadata
from omo_manager.omo_codex_stop import Args as CodexStopArgs
from omo_manager.omo_codex_stop import SHELL_COMMANDS
from omo_manager.omo_codex_stop import close_note
from omo_manager.omo_codex_stop import stop as guarded_codex_stop
from omo_manager.omo_report_receipt import bound_receipt_id, receipt_state_home
from omo_manager.omo_codex_status import SUPPORTED_CODEX_PACKAGES
from omo_manager.omo_codex_status import report_from_lines
from omo_manager.omo_completion_email import build_completion_email, completion_email_state_dir
from omo_manager.omo_task_lock import process_start_ticks, task_file_lock, task_target_lock
from omo_manager.omo_task_edit import render_pending_items
from omo_manager.omo_task_status import (
    Args as TaskStatusArgs,
    has_pending_marker,
    read_park_authority,
    read_park_authority_envelope,
    reconcile_todo_text,
    root_membership_lock,
    todo_row_task_paths,
    update_frontmatter_status,
)

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
FROZEN_MUTATION_COMMANDS = frozenset(
    {
        "apply",
        "reconcile-taskless-shell",
        "reconcile-completed-shell",
        "reconcile-unslop-done",
        "reconcile-absent-manager-history",
        "reconcile-source1385-live-worker-handoff",
    }
)
MUTABLE_HELPER_FROZEN_MESSAGE = (
    "namespace-drain helper is frozen: mutable packet lacks reviewed sealed runtime bootstrap"
)
GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}\Z")
PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
REPORT_AGENT_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
TARGET_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?\Z")
AUTHORITY_NAME = "85c5dff58359-1261.txt"
TRUSTED_ROOT = Path("/ssd1/sichangheagent/work_logs")
AUTHORITY_SHA256 = "8eff64c546c2d23e94a7d61135948341ddddd18a6bc0da5bf18e793b8724fd93"
AUTHORITY_LOCATOR = f"manager_mail/{AUTHORITY_NAME}:3-11"
AUTHORITY_DIRECTIVE = (
    "Do these now! I’m still seeing new emails from these agents that should have been closed and I don’t see mail count go down. It should be the opposite!!",
    "",
    "> On Aug 30, 2026, at 16:08, sichangheagent@gmail.com wrote:",
    "> ",
    "> Added pending items:",
    "> ",
    "> Close all opsmail0802 and agent_managers agents.",
    "> Consolidate their tasks and status and send one email summarizing them.",
    "> Let a new agent do mailbox compression.",
)
TRUSTED_RECEIPT_DIR = receipt_state_home() / "omo-manager" / "report-receipts"
PLAN_SCHEMA = "omo-namespace-drain-plan/v1"
REVIEW_SCHEMA = "omo-namespace-drain-review/v1"
LEDGER_SCHEMA = "omo-namespace-drain-ledger/v1"
DIRTY_MANIFEST_SCHEMA = "omo-namespace-drain-dirty-manifest/v1"
SHELL_BINDING_SCHEMA = "omo-namespace-drain-shell-binding/v1"
TASKLESS_SHELL_AUDIT_SCHEMA = "omo-namespace-drain-taskless-shell/v1"
COMPLETED_SHELL_AUDIT_SCHEMA = "omo-namespace-drain-completed-shell/v1"
SHELL_RECOVERY_SCHEMA = "omo-namespace-drain-shell-recovery/v1"
ABSENT_MANAGER_BINDING_SCHEMA = "omo-namespace-drain-absent-manager-history-binding/v1"
ABSENT_MANAGER_AUDIT_SCHEMA = "omo-namespace-drain-absent-manager-history/v1"
ABSENT_MANAGER_HISTORICAL_TARGET = "retired"
ABSENT_MANAGER_HISTORY_BLOCKER_PREFIX = "Source-1261 historical targetless custody: exact manager target absent"
ABSENT_MANAGER_PUBLIC_MUTATION_BLOCKER = (
    "absent-manager task/TODO mutation requires descriptor-bound public file compare-and-swap; unsupported before mutation"
)
SOURCE1385_BINDING_SCHEMA = "omo-namespace-drain-source1385-live-worker-handoff-binding/v1"
SOURCE1385_AUDIT_SCHEMA = "omo-namespace-drain-source1385-live-worker-handoff/v1"
SOURCE1385_TASK = "mail_compress_1261.md"
SOURCE1385_TARGET = "cedit:25"
SOURCE1385_MANAGER = "cedit:27"
SOURCE1385_OPERATION_BLOCKER = (
    "Source-1385 live-worker handoff execution requires a separately reviewed production packet"
)
SOURCE1385_PUBLIC_MUTATION_BLOCKER = (
    "Source-1385 handoff is non-executable: descriptor-bound task/TODO CAS and live-before-stop successor activation are unsupported"
)
SOURCE1385_CODE_REVIEW_SCHEMA = "omo-namespace-drain-source1385-code-review/v1"
SOURCE1385_AUTHORITY_NAME = "85c5dff58359-1386.txt"
SOURCE1385_AUTHORITY_SHA256 = "389e5ab4d0b316de05ee1a24e52f7c431d0f98bb6ecdfa5a375dacd976c8f12e"
SOURCE1385_AUTHORITY_TEXT = "Also close all cedit agents and move out their tasks"
SOURCE1385_PHASES = ("prepared", "kill-started", "pane-removed", "old-closed", "todo-moved", "successor-published", "complete")
STOPPABLE = {"error", "ready", "running", "stuck_input", "waiting_subagent"}
PASSIVE = {"absent", "not_codex"}
ALLOWED_PREFIXES = frozenset({"agent_managers", "opsmail0802"})
RENAME_EXCHANGE = 2
RENAME_NOREPLACE = 1
AT_EMPTY_PATH = 0x1000
AT_SYMLINK_FOLLOW = 0x400
AT_FDCWD = -100
TASKLESS_SHELL_TARGET = "opsmail0802:0.0"
TASKLESS_SHELL_CWD = "/ssd1/sichangheagent/opsmail0802"
COMPLETED_SHELL_TASK = "amh1232_term_eval.md"
COMPLETED_SHELL_TARGET = "agent_managers:39.0"
COMPLETED_SHELL_PANE = "%1855"
COMPLETED_SHELL_MESSAGE_ID = "<178811521612.3360518.2912986841064040014@gmail.com>"
COMPLETED_SHELL_BLOCKER = "done_close_failed: target is not a supported live Codex pane: %1855 status=not_codex"
UNSLOP_TASK = "unslop_skill_repair_1119.md"
UNSLOP_TASK_SHA256 = "02938b6dd5ec55b5fb3267872d90dc93563a43d756eb8f58faabd0d4c87c8c5c"
UNSLOP_TARGET = "wl:1"
UNSLOP_MANAGER = "wl:3"
UNSLOP_PANE = "%2"
UNSLOP_BLOCKER = "done_close_failed: refusing to stop the current pane: %2"
UNSLOP_ACCEPTED_EVIDENCE = (
    "(verified removed pending item: Restored the human-authored skill byte-for-byte, appended the current upstream "
    "Unslop body byte-for-byte except duplicate YAML frontmatter, independent reviewer PASS, validation PASS, and "
    "pushed .config macos commit b15d313; prior bad merge was dd428bb.)"
)
UNSLOP_RESULT_ROOT = Path("/home/sichangheagent/.config")
UNSLOP_RESULT_COMMIT = "b15d313329d872286e6d5b3b7090ba00ff4a8cfb"
UNSLOP_RESULT_REMOTE = "origin"
UNSLOP_RESULT_REMOTE_URL = "git@github.com:SichangHe/.config.git"
UNSLOP_RESULT_REMOTE_PROOF_URL = "https://github.com/SichangHe/.config.git"
UNSLOP_RESULT_REF = "refs/heads/macos"
UNSLOP_AUTHORITY_TEXT = "Reconcile unslop_skill_repair_1119.md to done without changing wl:1, sending mail, or removing queue items."
# No authenticated Human instruction currently authorizes this operation.
# A future reviewed code change may bind these five values to one already
# injected authoritative source/envelope.  They are deliberately not runtime
# inputs because agents share the UID that owns the files.
UNSLOP_AUTHORITY_SOURCE: str | None = None
UNSLOP_AUTHORITY_LINES: tuple[int, int] | None = None
UNSLOP_AUTHORITY_SOURCE_SHA256: str | None = None
UNSLOP_AUTHORITY_ENVELOPE: str | None = None
UNSLOP_AUTHORITY_ENVELOPE_SHA256: str | None = None
UNSLOP_BINDING_SCHEMA = "omo-namespace-drain-unslop-binding/v1"
UNSLOP_AUDIT_SCHEMA = "omo-namespace-drain-unslop-done/v1"
UNSLOP_GRAPH_CAS_SCHEMA = "omo-namespace-drain-unslop-graph-cas/v1"
CODE_REVIEW_SCHEMA = "omo-namespace-drain-code-review/v1"
CODE_REVIEW_TASK = "ns_code_review.md"
CODE_REVIEW_TARGET = "cedit:28"
TEST_PATH = Path(__file__).with_name("tests") / "test_namespace_drain.py"
DOC_PATH = Path(__file__).with_name("docs") / "routing" / "namespace-drain.md"
PIDFD_SYSCALLS = {"x86_64": (434, 424), "aarch64": (434, 424)}
RAW_ANCESTRY_MAX_COMMITS = 256
RAW_ANCESTRY_TIMEOUT_S = 15.0
ACTIVE_TASK_STATUSES = frozenset({"running", "long_running", "blocked"})
MANAGER_DIRECT_TASK_LIMIT = 4
MAX_GRAPH_TASK_BYTES = 4_000_000
MAX_PRIVATE_ARTIFACT_BYTES = 32_000_000
TASK_RECORD_KEYS = frozenset({"version", "status", "runat", "tool", "managerat", "is_manager", "pending_task_items"})
TASK_KEY_LINE_RE = re.compile(
    r"^[ \t]*(?:\"(?P<double>version|status|runat|tool|managerat|is_manager|pending_task_items)\"|"
    r"'(?P<single>version|status|runat|tool|managerat|is_manager|pending_task_items)'|"
    r"(?P<plain>version|status|runat|tool|managerat|is_manager|pending_task_items))[ \t]*:"
)
PROTECTED_DIRTY_PATHS = frozenset({b"0", b"0{n++}"})
GIT_EXECUTABLE = Path("/usr/bin/git")
GIT_EXECUTABLE_SHA256 = "587ef21868c948b883993e23209b86a72a6ddc06aab1545c697ffc31075acd4a"
GIT_EXECUTABLE_IDENTITY = (66306, 83888308, 0, 0o755, 3_710_360)
GIT_EXEC_PATH = Path("/usr/lib/git-core")
GIT_HTTPS_HELPER = GIT_EXEC_PATH / "git-remote-http"
GIT_HTTPS_HELPER_SHA256 = "ced8a184e12fbc8ed02eb92fc3edd79f762eb6ca7fd78b779da377f81306919e"
GIT_HTTPS_HELPER_IDENTITY = (66306, 84541602, 0, 0o755, 690_544)
GIT_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
GIT_CA_BUNDLE_SHA256 = "27273f0c6147c40533f5dd2c2ff20e4b145c8a04587c6cd87fbb16992879a244"
GIT_CA_BUNDLE_IDENTITY = (66306, 83099861, 0, 0o644, 182_140)
TMUX_EXECUTABLE = Path("/nix/store/v8kr4i8c12fjrsgh1r7v52vcpyfqy160-tmux-3.6a/bin/tmux")
TMUX_EXECUTABLE_SHA256 = "89c688f10e06baf9fe49724d6bef64c56354978b03e90414ccebbe0c80993b4d"
TMUX_EXECUTABLE_IDENTITY = (66306, 122204291, 0, 0o555, 1_301_920)
TMUX_SOCKET = Path("/tmp/tmux-30033/default")
TMUX_SOCKET_IDENTITY = (66306, 228731279, 30033, 0o660)
TMUX_SERVER_PID = 805677
TMUX_SERVER_START_TICKS = 605011547
TMUX_VERSION = "3.6a"


class DrainError(RuntimeError):
    """A drain invariant failed before or during a serialized transaction."""


class TaskExchangeError(DrainError):
    def __init__(self, path: Path, temporary_name: str, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.temporary_name = temporary_name


class ShellRemovalPartialError(DrainError):
    """Tmux accepted an irreversible exact-pane removal that needs recovery."""

    accepted = True


@dataclass(frozen=True)
class TaskSnapshot:
    path: str
    sha256: str
    status: str
    blocked_on: str
    runat: str
    managerat: str
    is_manager: bool
    pending_task_items: tuple[str, ...]
    source_base64: str


@dataclass(frozen=True)
class TargetSnapshot:
    target: str
    state: str
    tasks: tuple[str, ...]
    pane_id: str
    pane_pid: int
    pane_start_ticks: int


@dataclass(frozen=True)
class ShellProcessSnapshot:
    pid: int
    start_ticks: int
    state: str
    command: str
    cmdline_sha256: str
    executable: str
    executable_dev: int
    executable_ino: int
    executable_mode: int
    parent_pid: int
    session_id: int
    process_group_id: int
    foreground_process_group_id: int
    tty_device: int


@dataclass(frozen=True)
class ShellSnapshot:
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    command: str
    start_command_sha256: str
    capture_sha256: str
    root: ShellProcessSnapshot | None = None
    foreground: ShellProcessSnapshot | None = None
    current_path: str = ""
    host: str = ""
    history_size: int = 0
    pane_width: int = 0
    pane_height: int = 0
    cursor_x: int = 0
    cursor_y: int = 0


@dataclass(frozen=True)
class SharedPaneSnapshot:
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    pane_current_command: str
    agent_status: str
    launcher_pid: int
    launcher_start_ticks: int
    launcher_command: str
    launcher_cmdline_sha256: str
    launcher_executable: str
    launcher_executable_dev: int
    launcher_executable_ino: int
    agent_pid: int
    agent_start_ticks: int
    agent_command: str
    agent_cmdline_sha256: str
    agent_executable: str
    agent_executable_dev: int
    agent_executable_ino: int
    agent_lineage_sha256: str


@dataclass(frozen=True)
class ActiveGraphRow:
    path: str
    runat: str
    managerat: str
    is_manager: bool


@dataclass(frozen=True)
class DirectoryIdentity:
    path: str
    device: int
    inode: int
    uid: int
    mode: int


@dataclass(frozen=True)
class GraphRecordMetadata:
    status: str
    runat: str
    managerat: str
    is_manager: bool


@dataclass(frozen=True)
class TaskRecordRow:
    path: str
    device: int
    inode: int
    uid: int
    mode: int
    size: int
    sha256: str
    parents: tuple[DirectoryIdentity, ...]


@dataclass(frozen=True)
class ActiveGraphSnapshot:
    records: tuple[TaskRecordRow, ...]
    active_rows: tuple[ActiveGraphRow, ...]

    @property
    def task_record_paths(self) -> tuple[str, ...]:
        return tuple(record.path for record in self.records)

    @property
    def lock_generation_sha256(self) -> str:
        return sha256_bytes(canonical_json(list(self.task_record_paths)))


@dataclass
class PinnedDirectory:
    path: Path
    descriptors: list[int]
    states: list[os.stat_result]

    @property
    def descriptor(self) -> int:
        return self.descriptors[-1]

    def validate(self) -> None:
        validate_nofollow_directory_chain(self.path, self.descriptors, self.states, "recovery output")
        current = os.fstat(self.descriptor)
        if current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) & 0o077:
            raise DrainError("pinned recovery output directory is not owner-private")


@dataclass
class HeldPublishedAudit:
    published_fd: int
    staged_fd: int
    published_state: os.stat_result
    staged_state: os.stat_result

    def close(self) -> None:
        os.close(self.staged_fd)
        os.close(self.published_fd)


@dataclass
class PreparedShellRecovery:
    staged_audit: Path
    journal_path: Path
    identity: dict[str, object]
    phase: str
    staged_audit_fd: int | None
    staged_audit_state: os.stat_result | None
    journal_fd: int | None
    journal_state: os.stat_result | None
    phase_objects: dict[str, "HeldRecoveryPhaseObject"]

    def close(self) -> None:
        if self.staged_audit_fd is not None:
            os.close(self.staged_audit_fd)
            self.staged_audit_fd = None
        if self.journal_fd is not None:
            os.close(self.journal_fd)
            self.journal_fd = None
        for phase_object in self.phase_objects.values():
            phase_object.close()
        self.phase_objects.clear()


@dataclass
class HeldRecoveryPhaseObject:
    descriptor: int
    state: os.stat_result

    def close(self) -> None:
        os.close(self.descriptor)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DrainError(f"{path} must contain one JSON object")
    return value


def authority_digest(path: Path, expected: str, root: Path) -> str:
    expected_path = TRUSTED_ROOT / "manager_mail" / AUTHORITY_NAME
    if root.resolve() != TRUSTED_ROOT or path != expected_path or expected != AUTHORITY_SHA256:
        raise DrainError("Human authority must be the exact trusted Source-1261 record")
    data = private_file_bytes(path, expected, "Human authority")
    digest = sha256_bytes(data)
    lines = data.decode("utf-8").splitlines()
    if lines[:2] != ["Subject: Re: Close uncontrolled agents", ""] or tuple(lines[2:11]) != AUTHORITY_DIRECTIVE:
        raise DrainError("Human authority does not contain the exact namespace-close directive")
    return digest


def namespace_authority(root: Path) -> dict[str, str]:
    path = TRUSTED_ROOT / "manager_mail" / AUTHORITY_NAME
    digest = authority_digest(path, AUTHORITY_SHA256, root)
    return {"path": str(path), "locator": AUTHORITY_LOCATOR, "sha256": digest}


def validate_prefixes(prefixes: tuple[str, ...]) -> None:
    if len(prefixes) != len(ALLOWED_PREFIXES) or set(prefixes) != ALLOWED_PREFIXES:
        raise DrainError("Source-1261 requires exactly one opsmail0802 and one agent_managers prefix")
    for prefix in prefixes:
        if PREFIX_RE.fullmatch(prefix) is None:
            raise DrainError(f"invalid namespace prefix: {prefix}")
        if prefix not in ALLOWED_PREFIXES:
            raise DrainError(f"namespace is not authorized by Source-1261: {prefix}")


def is_owned(target: str, prefixes: tuple[str, ...]) -> bool:
    return any(target.startswith(f"{prefix}:") for prefix in prefixes)


def canonical_target(target: str) -> str:
    if TARGET_RE.fullmatch(target) is None:
        raise DrainError(f"invalid target: {target}")
    session, _, window_and_pane = target.partition(":")
    window, separator, pane = window_and_pane.partition(".")
    return f"{session}:{int(window)}.{int(pane) if separator else 0}"


def task_snapshot(root: Path, path: Path, prefixes: tuple[str, ...]) -> TaskSnapshot | None:
    data = path.read_bytes()
    try:
        metadata = parse_task_metadata(data.decode("utf-8"), work_log_root=root)
    except (UnicodeDecodeError, TaskFrontmatterError):
        decoded = data.decode("utf-8", errors="ignore")
        if any(re.search(rf"(?m)^\s*runat:\s*[\"']?{re.escape(prefix)}:", decoded) is not None for prefix in prefixes):
            raise DrainError(f"invalid namespace task metadata must not escape the plan: {path}")
        return None
    if metadata is None or not is_owned(metadata.runat, prefixes):
        return None
    if metadata.runat.startswith("h") or metadata.managerat.startswith("h"):
        raise DrainError(f"refusing human-owned target in {path}")
    return TaskSnapshot(
        path=str(path.relative_to(root)),
        sha256=sha256_bytes(data),
        status=metadata.status,
        blocked_on=metadata.blocked_on,
        runat=metadata.runat,
        managerat=metadata.managerat,
        is_manager=metadata.is_manager,
        pending_task_items=metadata.pending_task_items,
        source_base64=base64.b64encode(data).decode("ascii"),
    )


def trusted_file_identity(
    path: Path,
    expected_identity: tuple[int, int, int, int, int],
    expected_sha256: str,
    label: str,
) -> tuple[int, int, int, int, int, str]:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DrainError(f"cannot open the pinned {label} file") from error
    try:
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1_048_576):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        path_state = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise DrainError(f"cannot authenticate the pinned {label} file") from error
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_uid, stat.S_IMODE(before.st_mode), before.st_size)
    if (
        not stat.S_ISREG(before.st_mode)
        or identity != expected_identity
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        or (path_state.st_dev, path_state.st_ino) != (before.st_dev, before.st_ino)
        or size != before.st_size
        or digest.hexdigest() != expected_sha256
        or before.st_mode & 0o022
    ):
        raise DrainError(f"pinned {label} file identity drifted")
    return (*identity, expected_sha256)


def minimal_subprocess_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "SHELL": "/bin/sh",
        "TERM": "dumb",
        "TZ": "UTC",
    }


def tmux_route_identity() -> tuple[object, ...]:
    executable = trusted_file_identity(
        TMUX_EXECUTABLE,
        TMUX_EXECUTABLE_IDENTITY,
        TMUX_EXECUTABLE_SHA256,
        "tmux",
    )
    try:
        socket_state = TMUX_SOCKET.lstat()
        server_state = Path(f"/proc/{TMUX_SERVER_PID}").stat()
        server_executable = Path(f"/proc/{TMUX_SERVER_PID}/exe").stat()
        server_executable_path = os.readlink(f"/proc/{TMUX_SERVER_PID}/exe")
        server_start = process_start_ticks(TMUX_SERVER_PID)
        final_socket_state = TMUX_SOCKET.lstat()
    except OSError as error:
        raise DrainError("pinned tmux server route is unavailable") from error
    socket_identity = (
        socket_state.st_dev,
        socket_state.st_ino,
        socket_state.st_uid,
        stat.S_IMODE(socket_state.st_mode),
    )
    if (
        not stat.S_ISSOCK(socket_state.st_mode)
        or socket_identity != TMUX_SOCKET_IDENTITY
        or (final_socket_state.st_dev, final_socket_state.st_ino) != (socket_state.st_dev, socket_state.st_ino)
        or server_state.st_uid != os.getuid()
        or server_start != TMUX_SERVER_START_TICKS
        or server_executable_path != str(TMUX_EXECUTABLE)
        or (server_executable.st_dev, server_executable.st_ino) != TMUX_EXECUTABLE_IDENTITY[:2]
    ):
        raise DrainError("pinned tmux server/socket identity drifted")
    return (*executable, str(TMUX_SOCKET), *socket_identity, TMUX_SERVER_PID, TMUX_SERVER_START_TICKS, TMUX_VERSION)


def tmux_result(arguments: list[str], label: str) -> subprocess.CompletedProcess[bytes]:
    before = tmux_route_identity()
    try:
        result = subprocess.run(
            [str(TMUX_EXECUTABLE), "-f", "/dev/null", "-S", str(TMUX_SOCKET), *arguments],
            capture_output=True,
            timeout=5,
            check=False,
            env=minimal_subprocess_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DrainError(f"cannot invoke pinned tmux for {label}") from error
    if tmux_route_identity() != before:
        raise DrainError(f"pinned tmux route drifted during {label}")
    return result


def tmux_bytes(arguments: list[str], label: str, limit: int = 8_000_000) -> bytes:
    result = tmux_result(arguments, label)
    if result.returncode != 0:
        raise DrainError(f"cannot capture {label}: {result.stderr.decode(errors='replace').strip()}")
    if len(result.stdout) > limit:
        raise DrainError(f"{label} exceeded its capture bound")
    return result.stdout


def exact_pane_id(target: str) -> str:
    try:
        canonical = canonical_target(target)
        raw = tmux_bytes(
            ["list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}|#{pane_id}"],
            "complete exact-pane inventory",
        )
        text = raw.decode("utf-8")
    except (DrainError, UnicodeDecodeError):
        return ""
    matches: list[str] = []
    for line in text.splitlines():
        resolved, separator, pane_id = line.partition("|")
        if not separator or re.fullmatch(r"%\d+", pane_id) is None:
            return ""
        try:
            same_target = canonical_target(resolved) == canonical
        except DrainError:
            return ""
        if same_target:
            matches.append(pane_id)
    return matches[0] if len(matches) == 1 else ""


def inspect_target(target: str) -> str:
    # 🧑 "Close all opsmail0802 and agent_managers agents."
    pane_id = exact_pane_id(target)
    if not pane_id:
        return "absent"
    raw = tmux_bytes(["capture-pane", "-p", "-t", pane_id, "-S", "-80"], f"status tail for {target}")
    try:
        lines = [line.rstrip() for line in raw.decode("utf-8").splitlines()]
    except UnicodeDecodeError as error:
        raise DrainError(f"status tail is not UTF-8 for {target}") from error
    while lines and not lines[-1]:
        lines.pop()
    state = report_from_lines(lines).status
    if state == "not_codex":
        identity = tmux_bytes(
            ["display-message", "-p", "-t", pane_id, "#{pane_id}|#{pane_pid}|#{pane_current_command}"],
            f"managed-process identity for {target}",
        )
        try:
            resolved, raw_pid, command = identity.decode("utf-8").strip().split("|")
            if resolved == pane_id and raw_pid.isdigit() and command in {"bunx", "npx", "codex", "agent"}:
                _ = managed_agent_descendant(int(raw_pid), command)
                state = "running"
        except (UnicodeDecodeError, ValueError, DrainError):
            pass
    if exact_pane_id(target) != pane_id:
        raise DrainError(f"exact target drifted during state capture: {target}")
    if state not in STOPPABLE | PASSIVE:
        raise DrainError(f"unsupported target state for {target}: {state or 'unknown'}")
    return state


def target_identity(target: str, state: str) -> tuple[str, int, int]:
    if state == "absent":
        return "", 0, 0
    pane_id = exact_pane_id(target)
    if not pane_id:
        raise DrainError(f"target disappeared during identity capture: {target}")
    raw = tmux_bytes(
        ["display-message", "-p", "-t", pane_id, "#{pane_id}|#{pane_pid}"],
        f"target identity for {target}",
    )
    resolved_pane_id, separator, raw_pid = raw.decode("ascii").strip().partition("|")
    if not separator or resolved_pane_id != pane_id or not raw_pid.isdigit():
        raise DrainError(f"invalid target identity for {target}")
    pid = int(raw_pid)
    start_ticks = process_start_ticks(pid)
    if start_ticks is None or exact_pane_id(target) != pane_id:
        raise DrainError(f"cannot capture process start ticks for {target}")
    return pane_id, pid, start_ticks


def validate_trusted_root(root: Path) -> Path:
    resolved = root.resolve()
    if resolved != TRUSTED_ROOT.resolve():
        raise DrainError("reconciliation requires the exact trusted work-log root")
    details = resolved.lstat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.getuid():
        raise DrainError("trusted work-log root has unsafe identity")
    return resolved


def nofollow_absolute_file_bytes(path: Path, label: str, byte_limit: int = MAX_PRIVATE_ARTIFACT_BYTES) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute() or not path.parts or ".." in path.parts:
        raise DrainError(f"{label} path is not an absolute lexical path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fds: list[int] = []
    file_fd = -1
    try:
        directory_fds.append(os.open("/", directory_flags))
        for part in path.parts[1:-1]:
            directory_fds.append(os.open(part, directory_flags, dir_fd=directory_fds[-1]))
        file_fd = os.open(path.name, file_flags, dir_fd=directory_fds[-1])
        initial = os.fstat(file_fd)
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > byte_limit:
            raise DrainError(f"{label} is not a bounded no-follow regular file")
        with os.fdopen(os.dup(file_fd), "rb") as handle:
            data = handle.read(byte_limit + 1)
        final = os.fstat(file_fd)
        public = os.stat(path.name, dir_fd=directory_fds[-1], follow_symlinks=False)
        identity = (initial.st_dev, initial.st_ino, initial.st_mode, initial.st_uid, initial.st_size, initial.st_mtime_ns)
        if (
            len(data) > byte_limit
            or len(data) != initial.st_size
            or (final.st_dev, final.st_ino, final.st_mode, final.st_uid, final.st_size, final.st_mtime_ns) != identity
            or (public.st_dev, public.st_ino, public.st_mode, public.st_uid, public.st_size, public.st_mtime_ns) != identity
        ):
            raise DrainError(f"{label} identity drifted during no-follow capture")
        for index in range(1, len(directory_fds)):
            parent = os.fstat(directory_fds[index - 1])
            current = os.fstat(directory_fds[index])
            public_parent = os.stat(path.parts[index], dir_fd=directory_fds[index - 1], follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (public_parent.st_dev, public_parent.st_ino):
                raise DrainError(f"{label} parent identity drifted during no-follow capture")
            if not stat.S_ISDIR(parent.st_mode):
                raise DrainError(f"{label} has an unsafe parent identity")
        return data, initial
    except OSError as error:
        raise DrainError(f"cannot capture {label} without following paths: {error}") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def open_nofollow_directory_chain(path: Path, label: str) -> tuple[list[int], list[os.stat_result]]:
    if not path.is_absolute() or ".." in path.parts:
        raise DrainError(f"{label} is not an absolute lexical directory path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptors: list[int] = []
    states: list[os.stat_result] = []
    try:
        descriptors.append(os.open("/", flags))
        states.append(os.fstat(descriptors[-1]))
        for part in path.parts[1:]:
            descriptors.append(os.open(part, flags, dir_fd=descriptors[-1]))
            states.append(os.fstat(descriptors[-1]))
        validate_nofollow_directory_chain(path, descriptors, states, label)
        return descriptors, states
    except (OSError, DrainError):
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def validate_nofollow_directory_chain(
    path: Path,
    descriptors: list[int],
    states: list[os.stat_result],
    label: str,
) -> None:
    if len(descriptors) != len(states) or len(descriptors) != len(path.parts):
        raise DrainError(f"{label} directory chain is incomplete")
    for index, (descriptor, initial) in enumerate(zip(descriptors, states, strict=True)):
        current = os.fstat(descriptor)
        if not stat.S_ISDIR(current.st_mode) or directory_stat_identity(current) != directory_stat_identity(initial):
            raise DrainError(f"{label} retained directory identity drifted")
        if index:
            public = os.stat(path.parts[index], dir_fd=descriptors[index - 1], follow_symlinks=False)
            if directory_stat_identity(public) != directory_stat_identity(initial):
                raise DrainError(f"{label} public directory generation drifted")


@contextmanager
def pinned_directory(path: Path) -> Iterator[PinnedDirectory]:
    descriptors, states = open_nofollow_directory_chain(path, "recovery output")
    value = PinnedDirectory(path, descriptors, states)
    try:
        value.validate()
        yield value
        value.validate()
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def private_file_bytes(path: Path, expected_sha256: str, label: str) -> bytes:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise DrainError(f"{label} digest is invalid")
    data, details = nofollow_absolute_file_bytes(path, label)
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
        raise DrainError(f"{label} must be an owner-private no-follow regular file")
    if sha256_bytes(data) != expected_sha256:
        raise DrainError(f"{label} digest mismatch")
    return data


def authenticate_executor(executor: str) -> str:
    if TARGET_RE.fullmatch(executor) is None or executor.startswith("h") or is_owned(executor, tuple(ALLOWED_PREFIXES)):
        raise DrainError("executor must be an exact surviving non-Human target")
    pane_id = exact_pane_id(executor)
    if not pane_id or pane_id != os.environ.get("TMUX_PANE", ""):
        raise DrainError("executor is not authenticated by its exact current tmux pane")
    return pane_id


def validate_code_review(
    root: Path,
    review_path: Path,
    expected_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    executor: str,
) -> dict[str, object]:
    data = private_file_bytes(review_path, expected_sha256, "code review")
    try:
        review = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("code review is not valid JSON") from error
    if not isinstance(review, dict) or review.get("schema") != CODE_REVIEW_SCHEMA or review.get("verdict") != "PASS":
        raise DrainError("code review is not an exact PASS receipt")
    helper_data, _ = nofollow_absolute_file_bytes(Path(__file__).absolute(), "reviewed helper source")
    test_data, _ = nofollow_absolute_file_bytes(TEST_PATH.absolute(), "reviewed test source")
    doc_data, _ = nofollow_absolute_file_bytes(DOC_PATH.absolute(), "reviewed documentation source")
    helper_sha256 = sha256_bytes(helper_data)
    test_sha256 = sha256_bytes(test_data)
    doc_sha256 = sha256_bytes(doc_data)
    if (review.get("helper_sha256"), review.get("tests_sha256"), review.get("documentation_sha256")) != (helper_sha256, test_sha256, doc_sha256):
        raise DrainError("code review does not bind the current helper, tests, and documentation")
    reviewer_task = root / CODE_REVIEW_TASK
    reviewer_data = task_bytes_no_follow(root, reviewer_task)
    reviewer_sha256 = sha256_bytes(reviewer_data)
    reviewer = review.get("reviewer")
    if review.get("reviewer_task") != CODE_REVIEW_TASK or review.get("reviewer_task_sha256") != reviewer_sha256:
        raise DrainError("code review does not bind the exact reviewer task")
    metadata = parse_task_metadata(reviewer_data.decode("utf-8"), root)
    report_agent = review.get("report_agent")
    if (
        metadata is None
        or metadata.status not in {"running", "long_running", "blocked"}
        or reviewer != CODE_REVIEW_TARGET
        or metadata.runat != CODE_REVIEW_TARGET
        or not isinstance(reviewer, str)
        or reviewer == executor
        or reviewer.startswith("h")
        or is_owned(reviewer, tuple(ALLOWED_PREFIXES))
        or not isinstance(report_agent, str)
        or REPORT_AGENT_RE.fullmatch(report_agent) is None
    ):
        raise DrainError("code reviewer is not an active independent non-Human owner")
    consumed_data = private_file_bytes(consumed_receipt, consumed_receipt_sha256, "consumed code-review receipt")
    if consumed_receipt.resolve().parent != TRUSTED_RECEIPT_DIR.resolve():
        raise DrainError("consumed code-review receipt is outside the trusted report store")
    try:
        consumed = json.loads(consumed_data)
    except json.JSONDecodeError as error:
        raise DrainError("consumed code-review receipt is not valid JSON") from error
    consumed_input = consumed.get("input") if isinstance(consumed, dict) else None
    attestation = consumed.get("attestation_id") if isinstance(consumed, dict) else None
    unsigned = {key: value for key, value in consumed.items() if key != "attestation_id"} if isinstance(consumed, dict) else {}
    if (
        not isinstance(consumed, dict)
        or consumed.get("schema") != "omo-report-consumed-closure/v1"
        or consumed.get("accepted") is not False
        or consumed.get("terminal") is not True
        or not isinstance(consumed_input, dict)
        or consumed_input.get("file_sha256") != expected_sha256
        or attestation != bound_receipt_id(unsigned)
    ):
        raise DrainError("code review lacks exact consumed-report provenance")
    verify_consumed_report(review_path, reviewer, report_agent, str(consumed.get("status", "")), consumed_data, root)
    return review


def shell_children(pane_pid: int) -> tuple[int, ...]:
    raw = Path(f"/proc/{pane_pid}/task/{pane_pid}/children").read_text(encoding="ascii").split()
    if not all(value.isdigit() and int(value) > 0 for value in raw):
        raise DrainError("shell child-process inventory is invalid")
    return tuple(int(value) for value in raw)


def process_stat_fields(pid: int) -> tuple[str, int, int, int, int, int]:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as error:
        raise DrainError("shell process stat is unavailable") from error
    separator = raw.rfind(") ")
    fields = raw[separator + 2 :].split() if separator >= 0 else []
    if len(fields) < 6 or len(fields[0]) != 1 or not all(re.fullmatch(r"-?\d+", value) for value in fields[1:6]):
        raise DrainError("shell process stat is malformed")
    return (fields[0], *(int(value) for value in fields[1:6]))  # type: ignore[return-value]


def shell_process_snapshot(pid: int, accepted_states: frozenset[str] = frozenset({"S"})) -> ShellProcessSnapshot:
    process = Path(f"/proc/{pid}")
    try:
        process_state = process.stat()
        before_ticks = process_start_ticks(pid)
        process_status, parent_pid, process_group_id, session_id, tty_device, foreground_group = process_stat_fields(pid)
        command = (process / "comm").read_text(encoding="utf-8").strip().removeprefix("-")
        cmdline = (process / "cmdline").read_bytes()
        executable = os.readlink(process / "exe")
        executable_state = (process / "exe").stat()
        final_fields = process_stat_fields(pid)
        final_command = (process / "comm").read_text(encoding="utf-8").strip().removeprefix("-")
        final_cmdline = (process / "cmdline").read_bytes()
        final_executable = os.readlink(process / "exe")
        final_executable_state = (process / "exe").stat()
        after_ticks = process_start_ticks(pid)
    except (OSError, UnicodeError) as error:
        raise DrainError("shell process identity is unavailable") from error
    if (
        process_state.st_uid != os.getuid()
        or before_ticks is None
        or after_ticks != before_ticks
        or process_status not in accepted_states
        or final_fields != (process_status, parent_pid, process_group_id, session_id, tty_device, foreground_group)
        or final_command != command
        or final_cmdline != cmdline
        or final_executable != executable
        or (final_executable_state.st_dev, final_executable_state.st_ino, final_executable_state.st_mode)
        != (executable_state.st_dev, executable_state.st_ino, executable_state.st_mode)
        or command not in SHELL_COMMANDS
        or not cmdline
        or len(cmdline) > 1_000_000
        or executable_state.st_uid != 0
        or not stat.S_ISREG(executable_state.st_mode)
        or executable_state.st_mode & 0o022
        or os.path.basename(executable) != command
    ):
        raise DrainError("shell process identity is unsafe or drifted")
    arguments = cmdline.rstrip(b"\0").split(b"\0")
    if not arguments or os.path.basename(os.fsdecode(arguments[0])).removeprefix("-") != command:
        raise DrainError("shell process command line does not match its executable role")
    return ShellProcessSnapshot(
        pid,
        before_ticks,
        process_status,
        command,
        sha256_bytes(cmdline),
        executable,
        executable_state.st_dev,
        executable_state.st_ino,
        stat.S_IMODE(executable_state.st_mode),
        parent_pid,
        session_id,
        process_group_id,
        foreground_group,
        tty_device,
    )


def nested_shell_tree_snapshot(
    pane_pid: int,
    accepted_states: frozenset[str] = frozenset({"S"}),
) -> tuple[ShellProcessSnapshot, ShellProcessSnapshot]:
    children = shell_children(pane_pid)
    if len(children) != 1:
        raise DrainError("exact shell must have one and only one foreground shell child")
    child_pid = children[0]
    root = shell_process_snapshot(pane_pid, accepted_states)
    child = shell_process_snapshot(child_pid, accepted_states)
    if (
        root.parent_pid != TMUX_SERVER_PID
        or root.session_id != root.pid
        or root.process_group_id != root.pid
        or child.parent_pid != root.pid
        or child.session_id != root.session_id
        or child.process_group_id != child.pid
        or root.foreground_process_group_id != child.pid
        or child.foreground_process_group_id != child.pid
        or root.tty_device <= 0
        or child.tty_device != root.tty_device
        or shell_children(child.pid)
        or shell_children(root.pid) != (child.pid,)
        or shell_process_snapshot(root.pid, accepted_states) != root
        or shell_process_snapshot(child.pid, accepted_states) != child
    ):
        raise DrainError("exact nested shell tree is not the narrowly supported foreground topology")
    return root, child


def prompt_only_capture(raw: bytes, host: str, current_path: str) -> bool:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if any(character != "\n" and (ord(character) < 0x20 or ord(character) == 0x7F) for character in text):
        return False
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) != 2 or any(len(line) > 4096 for line in lines):
        return False
    suffix = f"{host} {current_path}"
    pattern = re.compile(rf"^❯ +{re.escape(suffix)}$")
    return all(pattern.fullmatch(line) is not None for line in lines)


def shell_snapshot(
    target: str,
    require_status: bool = True,
    require_empty_transcript: bool = False,
    process_states: frozenset[str] = frozenset({"S"}),
) -> ShellSnapshot:
    if require_status:
        state = inspect_target(target)
        if state != "not_codex":
            raise DrainError(f"exact target is not an exited shell: {target} status={state}")
    pane_id, pane_pid, pane_start_ticks = target_identity(target, "not_codex")
    raw_command = tmux_bytes(["display-message", "-p", "-t", pane_id, "#{pane_current_command}"], f"shell command for {target}")
    try:
        command = raw_command.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise DrainError(f"shell command is not UTF-8 for {target}") from error
    if command not in SHELL_COMMANDS:
        raise DrainError(f"exact target is not an idle supported shell: {target} command={command or 'empty'}")
    root_process, foreground_process = nested_shell_tree_snapshot(pane_pid, process_states)
    if command != foreground_process.command:
        raise DrainError(f"tmux foreground command does not match the bound shell child: {target}")
    raw_start = tmux_bytes(["display-message", "-p", "-t", pane_id, "#{pane_start_command}"], f"shell start command for {target}")
    raw_capture = tmux_bytes(["capture-pane", "-p", "-t", pane_id, "-S", "-"], f"shell transcript for {target}")
    raw_layout = tmux_bytes(
        [
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_current_path}|#{history_size}|#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}",
        ],
        f"shell pane layout for {target}",
    )
    try:
        current_path, raw_history, raw_width, raw_height, raw_cursor_x, raw_cursor_y = raw_layout.decode("utf-8").strip().split("|")
        layout_values = tuple(int(value) for value in (raw_history, raw_width, raw_height, raw_cursor_x, raw_cursor_y))
    except (UnicodeDecodeError, ValueError) as error:
        raise DrainError(f"shell pane layout is invalid for {target}") from error
    history_size, pane_width, pane_height, cursor_x, cursor_y = layout_values
    host = platform.node()
    if (
        require_empty_transcript
        and (
            target != TASKLESS_SHELL_TARGET
            or current_path != TASKLESS_SHELL_CWD
            or history_size != 1
            or pane_width <= 0
            or pane_height <= 0
            or (cursor_x, cursor_y) != (2, 0)
            or raw_start != b"\n"
            or not prompt_only_capture(raw_capture, host, current_path)
        )
    ):
        raise DrainError(f"taskless shell transcript/layout contains substantive or ambiguous activity: {target}")
    if (require_status and inspect_target(target) != "not_codex") or target_identity(target, "not_codex") != (pane_id, pane_pid, pane_start_ticks):
        raise DrainError(f"exact shell identity drifted during capture: {target}")
    if nested_shell_tree_snapshot(pane_pid, process_states) != (root_process, foreground_process):
        raise DrainError(f"exact nested shell tree drifted during capture: {target}")
    return ShellSnapshot(
        pane_id,
        pane_pid,
        pane_start_ticks,
        command,
        sha256_bytes(raw_start),
        sha256_bytes(raw_capture),
        root_process,
        foreground_process,
        current_path,
        host,
        history_size,
        pane_width,
        pane_height,
        cursor_x,
        cursor_y,
    )


def parse_shell_snapshot(raw: object) -> ShellSnapshot:
    if not isinstance(raw, dict):
        raise DrainError("shell binding is missing")
    try:
        raw_root = raw["root"]
        raw_foreground = raw["foreground"]
        if not isinstance(raw_root, dict) or not isinstance(raw_foreground, dict):
            raise TypeError

        def parse_process(value: dict[str, object]) -> ShellProcessSnapshot:
            return ShellProcessSnapshot(
                int(value["pid"]),
                int(value["start_ticks"]),
                str(value["state"]),
                str(value["command"]),
                str(value["cmdline_sha256"]),
                str(value["executable"]),
                int(value["executable_dev"]),
                int(value["executable_ino"]),
                int(value["executable_mode"]),
                int(value["parent_pid"]),
                int(value["session_id"]),
                int(value["process_group_id"]),
                int(value["foreground_process_group_id"]),
                int(value["tty_device"]),
            )

        snapshot = ShellSnapshot(
            str(raw["pane_id"]),
            int(raw["pane_pid"]),
            int(raw["pane_start_ticks"]),
            str(raw["command"]),
            str(raw["start_command_sha256"]),
            str(raw["capture_sha256"]),
            parse_process(raw_root),
            parse_process(raw_foreground),
            str(raw["current_path"]),
            str(raw["host"]),
            int(raw["history_size"]),
            int(raw["pane_width"]),
            int(raw["pane_height"]),
            int(raw["cursor_x"]),
            int(raw["cursor_y"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DrainError("shell binding has invalid fields") from error
    if (
        re.fullmatch(r"%\d+", snapshot.pane_id) is None
        or snapshot.pane_pid <= 0
        or snapshot.pane_start_ticks <= 0
        or snapshot.command not in SHELL_COMMANDS
        or SHA256_RE.fullmatch(snapshot.start_command_sha256) is None
        or SHA256_RE.fullmatch(snapshot.capture_sha256) is None
        or snapshot.root is None
        or snapshot.foreground is None
        or snapshot.root.pid != snapshot.pane_pid
        or snapshot.root.start_ticks != snapshot.pane_start_ticks
        or snapshot.foreground.parent_pid != snapshot.root.pid
        or snapshot.foreground.session_id != snapshot.root.session_id
        or snapshot.root.foreground_process_group_id != snapshot.foreground.pid
        or snapshot.foreground.foreground_process_group_id != snapshot.foreground.pid
        or snapshot.root.tty_device != snapshot.foreground.tty_device
        or snapshot.root.tty_device <= 0
        or not snapshot.current_path.startswith("/")
        or not snapshot.host
        or snapshot.history_size < 0
        or snapshot.pane_width <= 0
        or snapshot.pane_height <= 0
        or not (0 <= snapshot.cursor_x < snapshot.pane_width and 0 <= snapshot.cursor_y < snapshot.pane_height)
        or any(
            process.pid <= 0
            or process.start_ticks <= 0
            or process.state != "S"
            or process.command not in SHELL_COMMANDS
            or SHA256_RE.fullmatch(process.cmdline_sha256) is None
            or not process.executable.startswith("/")
            or process.executable_dev <= 0
            or process.executable_ino <= 0
            or process.executable_mode & 0o022
            for process in (snapshot.root, snapshot.foreground)
        )
    ):
        raise DrainError("shell binding has invalid identity values")
    return snapshot


def raw_runat_claims_namespace(decoded: str, target: str) -> bool:
    canonical = canonical_target(target)
    session = canonical.partition(":")[0]
    lines = decoded.splitlines()
    frontmatter = lines
    if lines and lines[0].strip() == "---":
        closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), len(lines))
        frontmatter = lines[1:closing]
    source = "\n".join(frontmatter)
    raw_claim = re.search(rf"(?m)^\s*(?:[\"']runat[\"']|runat)\s*:[^\r\n]*{re.escape(session)}:", decoded) is not None

    def claims_session(value: str) -> bool:
        scalar = value.strip()
        try:
            return canonical_target(scalar).partition(":")[0] == session
        except DrainError:
            return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(session)}:\d+(?:\.\d+)?(?![A-Za-z0-9_.-])", scalar) is not None

    try:
        document = yaml.compose(source, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        document = None
    if document is None:
        if claims_session(source):
            return True
        try:
            for token in yaml.scan(source, Loader=yaml.SafeLoader):
                if isinstance(token, ScalarToken) and isinstance(token.value, str) and claims_session(token.value):
                    return True
        except yaml.YAMLError:
            pass
        for match in re.finditer(r'"(?:[^"\\]|\\.)*"', source):
            try:
                scalar = yaml.load(match.group(0), Loader=yaml.SafeLoader)
            except yaml.YAMLError:
                continue
            if isinstance(scalar, str) and claims_session(scalar):
                return True
    document_runat = False

    def node_claims_runat(node: object, merged: bool = False, visited: set[int] | None = None) -> bool:
        nonlocal document_runat
        seen = visited if visited is not None else set()
        if id(node) in seen:
            return False
        seen.add(id(node))
        if isinstance(node, SequenceNode):
            return any(node_claims_runat(child, merged, seen) for child in node.value)
        if not isinstance(node, MappingNode):
            return merged
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                continue
            if key_node.value == "runat":
                document_runat = True
                if not isinstance(value_node, ScalarNode) or claims_session(value_node.value):
                    return True
            if key_node.value == "<<" or key_node.tag == "tag:yaml.org,2002:merge":
                if node_claims_runat(value_node, True, seen):
                    return True
        return False

    if node_claims_runat(document):
        return True
    if document_runat:
        return raw_claim

    key_pattern = re.compile(r"^(?P<indent>[ \t]*)(?:[\"']runat[\"']|runat)\s*:")
    for index, line in enumerate(frontmatter):
        match = key_pattern.match(line)
        if match is None:
            continue
        indentation = match.group("indent")
        if "\t" in indentation:
            return True
        indent = len(indentation)
        fragment = [line[indent:]]
        for continuation in frontmatter[index + 1 :]:
            if not continuation.strip():
                fragment.append(continuation)
                continue
            leading_whitespace = continuation[: len(continuation) - len(continuation.lstrip(" \t"))]
            if "\t" in leading_whitespace:
                return True
            continuation_indent = len(continuation) - len(continuation.lstrip(" "))
            if continuation_indent <= indent:
                break
            fragment.append(continuation[indent:])
        try:
            node = yaml.compose("\n".join(fragment), Loader=yaml.SafeLoader)
        except yaml.YAMLError:
            return True
        if not isinstance(node, MappingNode) or len(node.value) != 1:
            continue
        key_node, value_node = node.value[0]
        if not isinstance(key_node, ScalarNode) or key_node.value != "runat":
            continue
        if not isinstance(value_node, ScalarNode):
            return True
        if claims_session(value_node.value):
            return True
    return raw_claim


def task_records_for_target(root: Path, target: str, active_only: bool = False) -> tuple[Path, ...]:
    canonical = canonical_target(target)
    matches: list[Path] = []
    for candidate in sorted(root.rglob("*.md")):
        if "manager_mail" in candidate.parts:
            continue
        try:
            data, record = nofollow_record_snapshot(root, candidate)
        except (DrainError, OSError) as error:
            raise DrainError(f"unsafe or drifting Markdown record prevents complete target proof: {candidate}") from error
        decoded = data.decode("utf-8", errors="ignore")
        raw_claim = raw_runat_claims_namespace(decoded, target)
        try:
            metadata = parse_task_metadata(data.decode("utf-8"), root)
        except (UnicodeDecodeError, TaskFrontmatterError) as error:
            if raw_claim:
                raise DrainError(f"invalid task record claims exact target: {candidate}") from error
            continue
        if metadata is None:
            continue
        try:
            matches_target = canonical_target(metadata.runat) == canonical
        except DrainError:
            matches_target = False
        if matches_target and (not active_only or metadata.status != "done"):
            matches.append(root / record.path)
    return tuple(matches)


def pidfd_open(pane_pid: int) -> int:
    calls = PIDFD_SYSCALLS.get(platform.machine())
    if calls is None:
        raise DrainError("pidfd shell freezing is unsupported on this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(calls[0], pane_pid, 0)
    if result < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return int(result)


def pidfd_signal(pidfd: int, signal_number: int) -> None:
    calls = PIDFD_SYSCALLS.get(platform.machine())
    if calls is None:
        raise DrainError("pidfd shell freezing is unsupported on this architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(calls[1], pidfd, signal_number, 0, 0) < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def wait_pidfd_exits(pidfds: tuple[int, ...], timeout_s: float = 2.0) -> bool:
    watcher = select.poll()
    for pidfd in pidfds:
        watcher.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    remaining = set(pidfds)
    deadline = time.monotonic() + timeout_s
    while remaining:
        timeout_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if timeout_ms <= 0:
            return False
        for descriptor, events in watcher.poll(timeout_ms):
            if descriptor in remaining and events & (select.POLLIN | select.POLLHUP | select.POLLERR):
                remaining.remove(descriptor)
    return True


def shell_removal_attempt_state(
    target: str,
    expected: ShellSnapshot,
    result: subprocess.CompletedProcess[bytes] | None,
    expected_receipt: bytes,
    expected_rejection: bytes,
    require_empty_transcript: bool,
) -> str:
    if result is not None and result.returncode == 0 and result.stdout == expected_receipt:
        return "accepted"
    if result is not None and result.returncode == 0 and result.stdout == expected_rejection:
        return "rejected"
    return "unknown-after-attempt"


def guarded_remove_shell(target: str, expected: ShellSnapshot, require_empty_transcript: bool = False) -> None:
    if expected.root is None or expected.foreground is None:
        raise DrainError("exact nested shell binding is incomplete")
    if shell_snapshot(target, require_empty_transcript=require_empty_transcript) != expected:
        raise DrainError(f"exact shell binding drift before removal: {target}")
    root_pidfd: int | None = None
    foreground_pidfd: int | None = None
    stopped: list[int] = []
    result: subprocess.CompletedProcess[bytes] | None = None
    expected_receipt: bytes | None = None
    attempt_state = "not-attempted"
    attempt_error: DrainError | None = None
    process_tree_exited = False
    try:
        root_pidfd = pidfd_open(expected.root.pid)
        foreground_pidfd = pidfd_open(expected.foreground.pid)
        if process_start_ticks(expected.pane_pid) != expected.pane_start_ticks or target_identity(target, "not_codex") != (
            expected.pane_id,
            expected.pane_pid,
            expected.pane_start_ticks,
        ):
            raise DrainError(f"exact shell process drift before freeze: {target}")
        if nested_shell_tree_snapshot(expected.pane_pid) != (expected.root, expected.foreground):
            raise DrainError(f"complete nested shell tree drifted before freeze: {target}")
        pidfd_signal(foreground_pidfd, signal.SIGSTOP)
        stopped.append(foreground_pidfd)
        pidfd_signal(root_pidfd, signal.SIGSTOP)
        stopped.append(root_pidfd)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            states: list[str] = []
            for process_pid in (expected.root.pid, expected.foreground.pid):
                raw_stat = Path(f"/proc/{process_pid}/stat").read_text(encoding="utf-8")
                separator = raw_stat.rfind(") ")
                states.append(raw_stat[separator + 2 : separator + 3] if separator >= 0 else "")
            if states == ["T", "T"] or states == ["t", "t"] or all(state in {"T", "t"} for state in states):
                break
            time.sleep(0.01)
        else:
            raise DrainError(f"complete exact nested shell tree did not freeze: {target}")
        canonical = canonical_target(target)
        session, _, numeric = canonical.partition(":")
        window, _, pane = numeric.partition(".")
        predicates = [
            f"#{{==:#{{pane_id}},{expected.pane_id}}}",
            f"#{{==:#{{session_name}},{session}}}",
            f"#{{==:#{{window_index}},{window}}}",
            f"#{{==:#{{pane_index}},{pane}}}",
            f"#{{==:#{{pane_pid}},{expected.pane_pid}}}",
        ]
        base_condition = predicates[-1]
        for predicate in reversed(predicates[:-1]):
            base_condition = f"#{{&&:{predicate},{base_condition}}}"
        frozen = shell_snapshot(
            target,
            require_status=False,
            require_empty_transcript=require_empty_transcript,
            process_states=frozenset({"T", "t"}),
        )
        if frozen.root is None or frozen.foreground is None:
            raise DrainError(f"exact nested shell tree lost its process binding after freeze: {target}")
        normalized_frozen = replace(
            frozen,
            root=replace(frozen.root, state=expected.root.state),
            foreground=replace(frozen.foreground, state=expected.foreground.state),
        )
        if normalized_frozen != expected:
            raise DrainError(f"exact shell binding drift after freeze: {target}")
        command_condition = f"#{{==:#{{pane_current_command}},{expected.command}}}"
        condition = f"#{{&&:{base_condition},{command_condition}}}"
        nonce = f"{os.getpid()}_{time.monotonic_ns()}"
        accepted = f"OMO_SHELL_REMOVE_ACCEPTED_{nonce}"
        rejected = f"OMO_SHELL_REMOVE_REJECTED_{nonce}"
        expected_receipt = f"{accepted}\n".encode()
        expected_rejection = f"{rejected}\n".encode()
        success = f"{shlex.join(['display-message', '-p', accepted])} ; {shlex.join(['kill-pane', '-t', expected.pane_id])}"
        failure = shlex.join(["display-message", "-p", rejected])
        try:
            result = tmux_result(["if-shell", "-F", "-t", canonical, condition, success, failure], f"guarded removal of {target}")
        except DrainError as error:
            attempt_error = error
        attempt_state = shell_removal_attempt_state(target, expected, result, expected_receipt, expected_rejection, require_empty_transcript)
    finally:
        active_error = sys.exc_info()[1]
        cleanup_errors: list[str] = []
        for process_pidfd in reversed(stopped):
            try:
                pidfd_signal(process_pidfd, signal.SIGCONT)
            except OSError as error:
                if error.errno != 3:
                    cleanup_errors.append(f"resume pidfd {process_pidfd}: {error}")
        if attempt_state in {"accepted", "unknown-after-attempt"} and root_pidfd is not None and foreground_pidfd is not None:
            try:
                process_tree_exited = wait_pidfd_exits((foreground_pidfd, root_pidfd))
            except (OSError, ValueError) as error:
                cleanup_errors.append(f"wait for exact process-tree exit: {error}")
        for process_pidfd in (foreground_pidfd, root_pidfd):
            if process_pidfd is None:
                continue
            try:
                os.close(process_pidfd)
            except OSError as error:
                cleanup_errors.append(f"close pidfd {process_pidfd}: {error}")
        if cleanup_errors:
            detail = "; ".join(cleanup_errors)
            if attempt_state in {"accepted", "unknown-after-attempt"}:
                raise ShellRemovalPartialError(f"tmux accepted removal but exact process cleanup needs recovery: {detail}") from active_error
            if active_error is not None:
                active_error.add_note(f"nested-shell cleanup also failed: {detail}")
            else:
                raise DrainError(f"nested-shell cleanup failed: {detail}")
    if attempt_state == "unknown-after-attempt":
        raise ShellRemovalPartialError(f"exact-shell removal outcome is unknown after the tmux attempt: {target}") from attempt_error
    if attempt_state != "accepted" or result is None or expected_receipt is None:
        raise DrainError(f"guarded exact-shell removal was rejected: {target}")
    if exact_pane_id(target):
        raise DrainError(f"symbolic target unexpectedly remained or rebound after exact-shell removal: {target}")
    residual = tmux_result(
        ["display-message", "-p", "-t", expected.pane_id, "#{pane_id}"],
        f"post-removal pane absence for {target}",
    )
    if residual.returncode == 0:
        raise ShellRemovalPartialError(f"tmux accepted removal but exact pane still exists: {expected.pane_id}")
    if not process_tree_exited:
        raise ShellRemovalPartialError(f"tmux removed {target}, but its exact nested shell process tree did not exit")


def git_environment() -> dict[str, str]:
    return {
        **minimal_subprocess_environment(),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_EXEC_PATH": str(GIT_EXEC_PATH),
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    }


def git_result(
    root: Path | None,
    arguments: list[str],
    timeout: float = 30,
    pass_fds: tuple[int, ...] = (),
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    before = trusted_file_identity(GIT_EXECUTABLE, GIT_EXECUTABLE_IDENTITY, GIT_EXECUTABLE_SHA256, "Git")
    command = [
        str(GIT_EXECUTABLE),
        f"--exec-path={GIT_EXEC_PATH}",
        "--no-pager",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
    ]
    if root is not None:
        command.extend(["-C", str(root)])
    command.extend(arguments)
    try:
        result = subprocess.run(
            command,
            cwd="/",
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**git_environment(), **(extra_environment or {})},
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DrainError("cannot invoke the pinned Git executable") from error
    if trusted_file_identity(GIT_EXECUTABLE, GIT_EXECUTABLE_IDENTITY, GIT_EXECUTABLE_SHA256, "Git") != before:
        raise DrainError("pinned Git executable drifted during invocation")
    return result


def git_output(
    root: Path | None,
    arguments: list[str],
    pass_fds: tuple[int, ...] = (),
    extra_environment: dict[str, str] | None = None,
) -> bytes:
    result = git_result(root, arguments, pass_fds=pass_fds, extra_environment=extra_environment)
    if result.returncode != 0:
        raise DrainError(f"cannot capture repository state: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def git_remote_output(arguments: list[str]) -> bytes:
    transport = trusted_file_identity(
        GIT_HTTPS_HELPER,
        GIT_HTTPS_HELPER_IDENTITY,
        GIT_HTTPS_HELPER_SHA256,
        "Git HTTPS transport",
    )
    ca_bundle = trusted_file_identity(
        GIT_CA_BUNDLE,
        GIT_CA_BUNDLE_IDENTITY,
        GIT_CA_BUNDLE_SHA256,
        "Git CA bundle",
    )
    result = git_result(
        None,
        [
            "-c",
            "http.proxy=",
            "-c",
            "https.proxy=",
            "-c",
            f"http.sslCAInfo={GIT_CA_BUNDLE}",
            *arguments,
        ],
    )
    if result.returncode != 0:
        raise DrainError(f"cannot authenticate remote repository state: {result.stderr.decode(errors='replace').strip()}")
    if (
        trusted_file_identity(
            GIT_HTTPS_HELPER,
            GIT_HTTPS_HELPER_IDENTITY,
            GIT_HTTPS_HELPER_SHA256,
            "Git HTTPS transport",
        )
        != transport
        or trusted_file_identity(
            GIT_CA_BUNDLE,
            GIT_CA_BUNDLE_IDENTITY,
            GIT_CA_BUNDLE_SHA256,
            "Git CA bundle",
        )
        != ca_bundle
    ):
        raise DrainError("Git remote transport identity drifted")
    return result.stdout


def stable_stat_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def audit_link_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
        details.st_nlink,
    )


def directory_stat_identity(details: os.stat_result) -> tuple[int, int, int, int]:
    return details.st_dev, details.st_ino, details.st_mode, details.st_uid


def file_content_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return details.st_dev, details.st_ino, details.st_mode, details.st_uid, details.st_size, details.st_mtime_ns


def exchange_object_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    """Fields preserved when rename/exchange legitimately updates ctime."""
    return details.st_dev, details.st_ino, details.st_mode, details.st_uid, details.st_size


def link_bridge_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Fields that must survive hard-link publication except link metadata."""
    return details.st_dev, details.st_ino, details.st_mode, details.st_uid, details.st_size, details.st_mtime_ns


def exchange_object_value(details: os.stat_result) -> dict[str, int]:
    return {
        "device": details.st_dev,
        "inode": details.st_ino,
        "mode": details.st_mode,
        "uid": details.st_uid,
        "size": details.st_size,
    }


def exchange_object_matches(details: os.stat_result, expected: object) -> bool:
    return isinstance(expected, dict) and expected == exchange_object_value(details)


def read_bounded_fd(descriptor: int, byte_limit: int, label: str) -> tuple[bytes, os.stat_result]:
    initial = os.fstat(descriptor)
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > byte_limit:
        raise DrainError(f"{label} is not a bounded regular file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = byte_limit + 1
    while remaining:
        chunk = os.read(descriptor, min(1_048_576, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    final = os.fstat(descriptor)
    if len(data) > byte_limit or len(data) != initial.st_size or stable_stat_identity(final) != stable_stat_identity(initial):
        raise DrainError(f"{label} drifted during descriptor capture")
    return data, initial


def dirty_path_snapshot(root_fd: int, path_bytes: bytes, status_bytes: bytes) -> dict[str, object]:
    if path_bytes in PROTECTED_DIRTY_PATHS:
        raise DrainError("refusing to inspect an explicitly protected repository path")
    parts = path_bytes.split(b"/")
    if path_bytes.startswith(b"/") or not parts or any(part in {b"", b".", b".."} for part in parts):
        raise DrainError("repository status contains an unsafe path")
    directory_fds = [os.dup(root_fd)]
    directory_states = [os.fstat(root_fd)]
    try:
        for part in parts[:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory_fds[-1])
            state = os.fstat(descriptor)
            public = os.stat(part, dir_fd=directory_fds[-1], follow_symlinks=False)
            if not stat.S_ISDIR(state.st_mode) or directory_stat_identity(public) != directory_stat_identity(state):
                os.close(descriptor)
                raise DrainError("dirty repository path has a drifting parent")
            directory_fds.append(descriptor)
            directory_states.append(state)
        parent_fd = directory_fds[-1]
        name = parts[-1]
        try:
            details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                kind = "absent"
                digest = ""
                size = 0
                mode = 0
            else:
                raise DrainError("dirty repository path appeared during absence capture")
        else:
            mode = stat.S_IMODE(details.st_mode)
            size = details.st_size
            identity = stable_stat_identity(details)
            if stat.S_ISREG(details.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent_fd)
                try:
                    data, opened = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, "dirty repository file")
                finally:
                    os.close(descriptor)
                public = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stable_stat_identity(opened) != identity or stable_stat_identity(public) != identity:
                    raise DrainError("dirty repository file identity drifted")
                kind = "file"
                digest = sha256_bytes(data)
            elif stat.S_ISLNK(details.st_mode):
                first_target = os.readlink(name, dir_fd=parent_fd)
                public = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                second_target = os.readlink(name, dir_fd=parent_fd)
                if stable_stat_identity(public) != identity or second_target != first_target:
                    raise DrainError("dirty repository symlink identity drifted")
                kind = "symlink"
                digest = sha256_bytes(os.fsencode(first_target))
            else:
                raise DrainError(f"unsupported dirty repository path type: {os.fsdecode(path_bytes)}")
        for index, (descriptor, initial) in enumerate(zip(directory_fds, directory_states, strict=True)):
            if directory_stat_identity(os.fstat(descriptor)) != directory_stat_identity(initial):
                raise DrainError("dirty repository parent generation drifted during capture")
            if index:
                public_parent = os.stat(parts[index - 1], dir_fd=directory_fds[index - 1], follow_symlinks=False)
                if directory_stat_identity(public_parent) != directory_stat_identity(initial):
                    raise DrainError("dirty repository public parent generation drifted during capture")
    except OSError as error:
        raise DrainError(f"cannot capture dirty repository path without following it: {error}") from error
    finally:
        for descriptor in reversed(directory_fds):
            os.close(descriptor)
    return {
        "status_base64": base64.b64encode(status_bytes).decode("ascii"),
        "path_base64": base64.b64encode(path_bytes).decode("ascii"),
        "kind": kind,
        "mode": mode,
        "size": size,
        "sha256": digest,
    }


def repository_dirty_snapshot(root: Path, mutable_paths: tuple[Path, ...]) -> dict[str, object]:
    top = root
    excluded = {os.fsencode(path.relative_to(top)) for path in mutable_paths}
    root_chain, root_chain_states = open_nofollow_directory_chain(root, "work-log repository")
    root_fd = root_chain[-1]
    git_fd = -1
    index_fd = -1
    root_state = os.fstat(root_fd)
    try:
        git_fd = os.open(b".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=root_fd)
        git_state = os.fstat(git_fd)
        public_git = os.stat(b".git", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(git_state.st_mode) or directory_stat_identity(public_git) != directory_stat_identity(git_state):
            raise DrainError("work-log .git directory identity is unsafe or drifting")
        index_fd = os.open(b"index", os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=git_fd)
        index_data, index_state = read_bounded_fd(index_fd, MAX_PRIVATE_ARTIFACT_BYTES, "Git index")
        public_index = os.stat(b"index", dir_fd=git_fd, follow_symlinks=False)
        if stable_stat_identity(public_index) != stable_stat_identity(index_state):
            raise DrainError("Git index public identity drifted")
        git_arguments = [
            f"--git-dir=/proc/self/fd/{git_fd}",
            f"--work-tree=/proc/self/fd/{root_fd}",
            "status",
            "--porcelain=v1",
            "-z",
            "--no-renames",
            "--untracked-files=all",
        ]
        git_descriptors = (root_fd, git_fd, index_fd)
        git_index_environment = {"GIT_INDEX_FILE": f"/proc/self/fd/{index_fd}"}

        def porcelain_records() -> tuple[tuple[bytes, bytes, bytes], ...]:
            validate_nofollow_directory_chain(root, root_chain, root_chain_states, "work-log repository")
            raw = git_output(None, git_arguments, git_descriptors, git_index_environment)
            validate_nofollow_directory_chain(root, root_chain, root_chain_states, "work-log repository")
            if (
                directory_stat_identity(os.stat(b".git", dir_fd=root_fd, follow_symlinks=False)) != directory_stat_identity(git_state)
                or stable_stat_identity(os.stat(b"index", dir_fd=git_fd, follow_symlinks=False)) != stable_stat_identity(index_state)
            ):
                raise DrainError("descriptor-bound Git route drifted during status capture")
            records: list[tuple[bytes, bytes, bytes]] = []
            seen_paths: set[bytes] = set()
            for record in raw.split(b"\0"):
                if not record:
                    continue
                if len(record) < 4 or record[2:3] != b" ":
                    raise DrainError("repository status has an unsupported porcelain record")
                status_bytes = record[:2]
                path_bytes = record[3:]
                if b"R" in status_bytes or b"C" in status_bytes:
                    raise DrainError("repository status contains a forbidden rename/copy record")
                if path_bytes in seen_paths:
                    raise DrainError("repository status contains a duplicate path")
                seen_paths.add(path_bytes)
                records.append((record, status_bytes, path_bytes))
            return tuple(records)

        def capture(records: tuple[tuple[bytes, bytes, bytes], ...]) -> tuple[bytes, list[dict[str, object]]]:
            entries: list[dict[str, object]] = []
            filtered: list[bytes] = []
            for record, status_bytes, path_bytes in records:
                if path_bytes in PROTECTED_DIRTY_PATHS:
                    filtered.append(record)
                    entries.append(
                        {
                            "status_base64": base64.b64encode(status_bytes).decode("ascii"),
                            "path_base64": base64.b64encode(path_bytes).decode("ascii"),
                            "kind": "content_uninspected",
                            "mode": 0,
                            "size": 0,
                            "sha256": "",
                        }
                    )
                    continue
                if path_bytes in excluded:
                    continue
                filtered.append(record)
                entries.append(dirty_path_snapshot(root_fd, path_bytes, status_bytes))
            return b"\0".join(filtered) + (b"\0" if filtered else b""), entries

        first_records = porcelain_records()
        second_records = porcelain_records()
        if second_records != first_records:
            raise DrainError("repository status drifted before dirty-path inspection")
        status_data, entries = capture(first_records)
        final_records = porcelain_records()
        status_check, entries_check = capture(final_records)
        final_index_data, final_index_state = read_bounded_fd(index_fd, MAX_PRIVATE_ARTIFACT_BYTES, "Git index")
        final_public_index = os.stat(b"index", dir_fd=git_fd, follow_symlinks=False)
        final_public_git = os.stat(b".git", dir_fd=root_fd, follow_symlinks=False)
        if (
            status_check != status_data
            or entries_check != entries
            or final_index_data != index_data
            or stable_stat_identity(final_index_state) != stable_stat_identity(index_state)
            or stable_stat_identity(final_public_index) != stable_stat_identity(index_state)
            or directory_stat_identity(os.fstat(git_fd)) != directory_stat_identity(git_state)
            or directory_stat_identity(final_public_git) != directory_stat_identity(git_state)
            or directory_stat_identity(os.fstat(root_fd)) != directory_stat_identity(root_state)
        ):
            raise DrainError("repository state drifted during dirty-path capture")
        validate_nofollow_directory_chain(root, root_chain, root_chain_states, "work-log repository")
        return {
            "schema": DIRTY_MANIFEST_SCHEMA,
            "root": str(top),
            "root_identity": {"st_dev": root_state.st_dev, "st_ino": root_state.st_ino},
            "git_dir": {"path": str(root / ".git"), "st_dev": git_state.st_dev, "st_ino": git_state.st_ino},
            "index": {
                "path": str(root / ".git" / "index"),
                "st_dev": index_state.st_dev,
                "st_ino": index_state.st_ino,
                "sha256": sha256_bytes(index_data),
                "size": len(index_data),
            },
            "status_base64": base64.b64encode(status_data).decode("ascii"),
            "entries": entries,
            "excluded_mutable_paths": [str(path.relative_to(top)) for path in mutable_paths],
        }
    finally:
        if index_fd >= 0:
            os.close(index_fd)
        if git_fd >= 0:
            os.close(git_fd)
        for descriptor in reversed(root_chain):
            os.close(descriptor)


def validate_dirty_snapshot(root: Path, mutable_paths: tuple[Path, ...], expected: object) -> dict[str, object]:
    if not isinstance(expected, dict) or expected.get("schema") != DIRTY_MANIFEST_SCHEMA:
        raise DrainError("dirty-path manifest is invalid")
    current = repository_dirty_snapshot(root, mutable_paths)
    if current != expected:
        raise DrainError("unrelated repository or index state drifted")
    return current


def managed_agent_process_snapshot(pid: int, expected_command: str) -> tuple[int, int, str, str, str, int, int] | None:
    process = Path(f"/proc/{pid}")
    try:
        state = process.stat()
        start_ticks = process_start_ticks(pid)
        cmdline = (process / "cmdline").read_bytes()
        command = (process / "comm").read_text(encoding="utf-8").strip()
        executable = os.readlink(process / "exe")
        executable_state = (process / "exe").stat()
        final_start_ticks = process_start_ticks(pid)
    except (OSError, UnicodeError):
        return None
    if state.st_uid != os.getuid() or start_ticks is None or final_start_ticks != start_ticks or not cmdline or len(cmdline) > 1_000_000:
        return None
    arguments = [os.fsdecode(value) for value in cmdline.rstrip(b"\0").split(b"\0")]
    if not arguments or os.path.basename(arguments[0]) != expected_command or command != expected_command:
        return None
    if expected_command in {"bunx", "npx"}:
        if len(arguments) < 2 or arguments[1] not in SUPPORTED_CODEX_PACKAGES:
            return None
    elif expected_command not in {"codex", "agent"}:
        return None
    return (
        pid,
        start_ticks,
        command,
        sha256_bytes(cmdline),
        executable,
        executable_state.st_dev,
        executable_state.st_ino,
    )


def managed_agent_descendant(
    pane_pid: int,
    expected_command: str,
) -> tuple[int, int, str, str, str, int, int, int, int, str, str, str, int, int, str]:
    pending: list[tuple[int, int, tuple[int, ...]]] = [(pane_pid, 0, (pane_pid,))]
    seen = {pane_pid}
    lineages: dict[int, tuple[int, ...]] = {pane_pid: (pane_pid,)}
    launcher_candidates: list[tuple[int, tuple[int, int, str, str, str, int, int], tuple[int, ...]]] = []
    while pending:
        parent, depth, lineage = pending.pop(0)
        if len(seen) > 512 or depth > 32:
            raise DrainError("Unslop shared pane process tree exceeds its safety bound")
        try:
            children = shell_children(parent)
        except OSError as error:
            raise DrainError("Unslop shared pane process tree drifted during capture") from error
        for child in children:
            if child in seen:
                raise DrainError("Unslop shared pane process tree contains a cycle")
            seen.add(child)
            child_lineage = (*lineage, child)
            lineages[child] = child_lineage
            candidate = managed_agent_process_snapshot(child, expected_command)
            if candidate is not None:
                launcher_candidates.append((depth + 1, candidate, child_lineage))
            pending.append((child, depth + 1, child_lineage))
    if not launcher_candidates:
        raise DrainError("Unslop shared pane has no live managed-agent descendant")
    shallowest = min(depth for depth, _candidate, _lineage in launcher_candidates)
    launchers = [(candidate, lineage) for depth, candidate, lineage in launcher_candidates if depth == shallowest]
    if len(launchers) != 1:
        raise DrainError("Unslop shared pane has ambiguous managed-agent descendants")
    launcher, launcher_lineage = launchers[0]
    candidate, lineage = launcher, launcher_lineage
    if expected_command in {"bunx", "npx"}:
        codex_candidates = [
            (managed, child_lineage)
            for child, child_lineage in lineages.items()
            if launcher[0] in child_lineage and child != launcher[0] and (managed := managed_agent_process_snapshot(child, "codex")) is not None
        ]
        if len(codex_candidates) != 1:
            raise DrainError("Unslop shared pane lacks one exact Codex descendant")
        candidate, lineage = codex_candidates[0]

    def lineage_state() -> tuple[tuple[int, int], ...]:
        values: list[tuple[int, int]] = []
        for pid in lineage:
            start_ticks = process_start_ticks(pid)
            if start_ticks is None:
                raise DrainError("Unslop shared managed-agent lineage drifted during capture")
            values.append((pid, start_ticks))
        return tuple(values)

    first_lineage_state = lineage_state()
    if lineage_state() != first_lineage_state or first_lineage_state[-1][1] != candidate[1]:
        raise DrainError("Unslop shared managed-agent lineage drifted during capture")
    lineage_bytes = b"\0".join(f"{pid}:{start_ticks}".encode("ascii") for pid, start_ticks in first_lineage_state)
    return (*launcher, *candidate, sha256_bytes(lineage_bytes))


def shared_pane_snapshot() -> SharedPaneSnapshot:
    agent_status = inspect_target(UNSLOP_TARGET)
    if agent_status not in {"ready", "running"}:
        raise DrainError("Unslop shared target is not a live managed agent")
    pane_id, pane_pid, pane_start_ticks = target_identity(UNSLOP_TARGET, agent_status)
    raw = tmux_bytes(
        ["display-message", "-p", "-t", pane_id, "#{pane_id}|#{pane_pid}|#{pane_current_command}"],
        "Unslop shared pane identity",
    )
    try:
        resolved_id, raw_pid, pane_command = raw.decode("utf-8").strip().split("|")
    except (UnicodeDecodeError, ValueError) as error:
        raise DrainError("Unslop shared pane metadata is invalid") from error
    if pane_id != UNSLOP_PANE or resolved_id != pane_id or not raw_pid.isdigit() or int(raw_pid) != pane_pid or pane_command not in {"bunx", "npx", "codex", "agent"}:
        raise DrainError("Unslop shared target is not the exact authorized managed-agent pane")
    agent = managed_agent_descendant(pane_pid, pane_command)
    snapshot = SharedPaneSnapshot(
        pane_id,
        pane_pid,
        pane_start_ticks,
        pane_command,
        agent_status,
        *agent,
    )
    if (
        inspect_target(UNSLOP_TARGET) != agent_status
        or target_identity(UNSLOP_TARGET, agent_status) != (pane_id, pane_pid, pane_start_ticks)
        or managed_agent_descendant(pane_pid, pane_command) != agent
    ):
        raise DrainError("Unslop shared managed-agent identity drifted during capture")
    return snapshot


def raw_commit_contains_ancestor(root: Path, tip: str, ancestor: str) -> None:
    pending = [tip]
    visited: set[str] = set()
    total_bytes = 0
    deadline = time.monotonic() + RAW_ANCESTRY_TIMEOUT_S
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        if len(visited) >= RAW_ANCESTRY_MAX_COMMITS:
            raise DrainError("Unslop result ancestry exceeds its commit bound")
        visited.add(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DrainError("Unslop result ancestry exceeds its time bound")
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "cat-file", "commit", current],
                capture_output=True,
                timeout=remaining,
                check=False,
                env={
                    **os.environ,
                    "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
                },
            )
        except subprocess.TimeoutExpired as error:
            raise DrainError("Unslop result ancestry exceeds its time bound") from error
        if result.returncode != 0:
            raise DrainError(f"cannot read raw Unslop commit object: {result.stderr.decode(errors='replace').strip()}")
        payload = result.stdout
        total_bytes += len(payload)
        if len(payload) > 10_000_000 or total_bytes > 256_000_000:
            raise DrainError("Unslop result ancestry exceeds its byte bound")
        object_header = f"commit {len(payload)}\0".encode("ascii")
        object_id = hashlib.sha1(object_header + payload, usedforsecurity=False).hexdigest()
        if object_id != current:
            raise DrainError("Unslop result raw commit object does not match its identity")
        header, separator, _message = payload.partition(b"\n\n")
        if not separator or not header.startswith(b"tree "):
            raise DrainError("Unslop result commit object is malformed")
        if current == ancestor:
            return
        parents: list[str] = []
        for line in header.splitlines():
            if not line.startswith(b"parent "):
                continue
            parent = line.removeprefix(b"parent ")
            try:
                decoded = parent.decode("ascii")
            except UnicodeDecodeError as error:
                raise DrainError("Unslop result commit parent is malformed") from error
            if GIT_OBJECT_RE.fullmatch(decoded) is None:
                raise DrainError("Unslop result commit parent is malformed")
            parents.append(decoded)
        pending.extend(reversed(parents))
    raise DrainError("Unslop completed result is not in the authenticated remote branch")


def result_git_guard(root: Path, git_dir: Path, common_dir: Path) -> tuple[int, int]:
    for label, directory in (("Git", git_dir), ("Git common", common_dir)):
        directory_state = directory.lstat()
        if stat.S_ISLNK(directory_state.st_mode) or not stat.S_ISDIR(directory_state.st_mode) or directory_state.st_uid != os.getuid():
            raise DrainError(f"Unslop result {label} directory has unsafe identity")
    if common_dir != git_dir:
        raise DrainError("Unslop result repository uses an unsupported split Git common directory")
    if os.path.lexists(common_dir / "info" / "grafts"):
        raise DrainError("Unslop result repository has a legacy graft file")
    if os.path.lexists(common_dir / "shallow"):
        raise DrainError("Unslop result repository is shallow")
    if git_output(root, ["for-each-ref", "--format=%(refname)", "refs/replace"]).strip():
        raise DrainError("Unslop result repository has replacement refs")
    state = common_dir.lstat()
    return state.st_dev, state.st_ino


def unslop_result_snapshot() -> dict[str, object]:
    root = UNSLOP_RESULT_ROOT.resolve()
    details = root.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise DrainError("Unslop result repository has unsafe identity")
    top = Path(os.fsdecode(git_output(root, ["rev-parse", "--show-toplevel"]).rstrip(b"\n"))).resolve()
    if top != root:
        raise DrainError("Unslop result repository is not the exact Git top level")
    git_dir = Path(os.fsdecode(git_output(root, ["rev-parse", "--absolute-git-dir"]).rstrip(b"\n"))).resolve()
    common_value = Path(os.fsdecode(git_output(root, ["rev-parse", "--git-common-dir"]).rstrip(b"\n")))
    common_dir = (root / common_value).resolve() if not common_value.is_absolute() else common_value.resolve()
    object_format = git_output(root, ["rev-parse", "--show-object-format"]).decode("ascii").strip()
    if object_format != "sha1":
        raise DrainError("Unslop result repository does not use the required SHA-1 object format")
    git_identity = result_git_guard(root, git_dir, common_dir)
    object_type = git_output(root, ["cat-file", "-t", UNSLOP_RESULT_COMMIT]).decode("ascii").strip()
    object_id = git_output(root, ["rev-parse", "--verify", f"{UNSLOP_RESULT_COMMIT}^{{commit}}"]).decode("ascii").strip()
    if object_type != "commit" or object_id != UNSLOP_RESULT_COMMIT:
        raise DrainError("Unslop result is not the exact full commit object")
    remote_url = git_output(root, ["remote", "get-url", UNSLOP_RESULT_REMOTE]).decode("utf-8").strip()
    if remote_url != UNSLOP_RESULT_REMOTE_URL:
        raise DrainError("Unslop result repository remote identity drifted")
    remote_lines = (
        git_output(
            root,
            ["ls-remote", "--exit-code", "--refs", UNSLOP_RESULT_REMOTE_URL, UNSLOP_RESULT_REF],
        )
        .decode("ascii")
        .splitlines()
    )
    expected_suffix = f"\t{UNSLOP_RESULT_REF}"
    matches = [line.removesuffix(expected_suffix) for line in remote_lines if line.endswith(expected_suffix)]
    if len(matches) != 1 or GIT_OBJECT_RE.fullmatch(matches[0]) is None:
        raise DrainError("Unslop result remote branch has ambiguous identity")
    remote_tip = matches[0]
    raw_commit_contains_ancestor(root, remote_tip, UNSLOP_RESULT_COMMIT)
    if result_git_guard(root, git_dir, common_dir) != git_identity:
        raise DrainError("Unslop result Git metadata drifted during ancestry proof")
    if git_output(root, ["rev-parse", "--show-object-format"]).decode("ascii").strip() != object_format:
        raise DrainError("Unslop result object format drifted during ancestry proof")
    if git_output(root, ["remote", "get-url", UNSLOP_RESULT_REMOTE]).decode("utf-8").strip() != remote_url:
        raise DrainError("Unslop result repository remote identity drifted during proof")
    final_remote_lines = (
        git_output(
            root,
            ["ls-remote", "--exit-code", "--refs", UNSLOP_RESULT_REMOTE_URL, UNSLOP_RESULT_REF],
        )
        .decode("ascii")
        .splitlines()
    )
    if final_remote_lines != remote_lines:
        raise DrainError("Unslop result remote branch drifted during ancestry proof")
    tree = git_output(root, ["rev-parse", "--verify", f"{UNSLOP_RESULT_COMMIT}^{{tree}}"]).decode("ascii").strip()
    if GIT_OBJECT_RE.fullmatch(tree) is None:
        raise DrainError("Unslop result commit has invalid identity")
    return {
        "root": str(root),
        "git_common_dir": str(common_dir),
        "git_common_dir_identity": {"st_dev": git_identity[0], "st_ino": git_identity[1]},
        "object_format": object_format,
        "commit": UNSLOP_RESULT_COMMIT,
        "tree": tree,
        "remote": UNSLOP_RESULT_REMOTE,
        "remote_url": remote_url,
        "remote_ref": UNSLOP_RESULT_REF,
        "remote_tip": remote_tip,
        "ancestry_policy": {
            "replacement_refs": "absent",
            "legacy_grafts": "absent",
            "shallow_boundary": "absent",
        },
        "dirty_manifest": repository_dirty_snapshot(root, ()),
    }


def unslop_task_replacement(root: Path, task_data: bytes) -> bytes:
    if sha256_bytes(task_data) != UNSLOP_TASK_SHA256:
        raise DrainError("Unslop task does not match the exact authorized bytes")
    try:
        text = task_data.decode("utf-8")
        metadata = parse_task_metadata(text, root)
    except (UnicodeDecodeError, TaskFrontmatterError) as error:
        raise DrainError("Unslop task metadata is invalid") from error
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.blocked_on != UNSLOP_BLOCKER
        or metadata.runat != UNSLOP_TARGET
        or metadata.tool != "codex"
        or metadata.managerat != UNSLOP_MANAGER
        or metadata.is_manager
        or metadata.pending_task_items
        or has_pending_marker(text)
        or text.count(UNSLOP_ACCEPTED_EVIDENCE) != 1
    ):
        raise DrainError("Unslop task does not match the exact accepted queue-empty completion state")
    return update_frontmatter_status(text, "done", "", root).encode()


def graph_target(target: str) -> str:
    return target if target == "retired" else canonical_target(target)


def plausible_task_keys(text: str) -> frozenset[str]:
    keys: set[str] = set()
    fenced = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or stripped.startswith(">"):
            continue
        match = TASK_KEY_LINE_RE.match(line)
        if match is not None:
            keys.add(next(value for value in match.groups() if value is not None))
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            if isinstance(token, ScalarToken) and token.value in TASK_RECORD_KEYS:
                keys.add(token.value)
    except yaml.YAMLError:
        pass
    return frozenset(keys)


def task_signature_is_plausible(text: str) -> bool:
    keys = plausible_task_keys(text)
    return {"status", "runat", "managerat"} <= keys and len(keys & TASK_RECORD_KEYS) >= 4


def graph_record_metadata(data: bytes, root: Path, path: Path) -> GraphRecordMetadata | None:
    if len(data) > MAX_GRAPH_TASK_BYTES:
        raise DrainError(f"task-like Markdown record exceeds the graph/CAS byte bound: {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        if task_signature_is_plausible(data.decode("utf-8", errors="ignore")):
            raise DrainError(f"plausible task record is not UTF-8: {path}") from error
        return None
    try:
        metadata = parse_task_metadata(text, root)
    except TaskFrontmatterError as error:
        try:
            source = frontmatter_text(text)
        except TaskFrontmatterError:
            source = text.removeprefix("---\n") if text.startswith("---\n") else ""
        if not task_signature_is_plausible(source or text):
            return None
        try:
            values = yaml.load(source, Loader=UniqueKeyLoader) if source is not None else None
        except (TypeError, ValueError, yaml.YAMLError) as yaml_error:
            raise DrainError(f"plausible task record has malformed frontmatter: {path}") from yaml_error
        if (
            isinstance(values, dict)
            and TASK_RECORD_KEYS <= values.keys()
            and values.get("status") == "done"
            and isinstance(values.get("runat"), str)
            and isinstance(values.get("managerat"), str)
            and isinstance(values.get("is_manager"), bool)
        ):
            return GraphRecordMetadata("done", str(values["runat"]), str(values["managerat"]), bool(values["is_manager"]))
        raise DrainError(f"plausible active task record has invalid frontmatter: {path}") from error
    if metadata is None:
        if task_signature_is_plausible(text):
            raise DrainError(f"plausible task record lacks valid authoritative frontmatter: {path}")
        return None
    return GraphRecordMetadata(metadata.status, metadata.runat, metadata.managerat, metadata.is_manager)


def nofollow_record_snapshot(root: Path, path: Path) -> tuple[bytes, TaskRecordRow]:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise DrainError(f"graph/CAS task path is outside the trusted root: {path}") from error
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise DrainError(f"graph/CAS task path is unsafe: {path}")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fds: list[int] = []
    directory_names: list[str] = []
    file_fd = -1
    try:
        directory_fds.append(os.open(root, directory_flags))
        directory_names.append(".")
        for index, part in enumerate(relative.parts[:-1]):
            directory_fds.append(os.open(part, directory_flags, dir_fd=directory_fds[-1]))
            directory_names.append(Path(*relative.parts[: index + 1]).as_posix())
        file_fd = os.open(relative.name, file_flags, dir_fd=directory_fds[-1])
        file_state = os.fstat(file_fd)
        if not stat.S_ISREG(file_state.st_mode) or file_state.st_uid != os.getuid():
            raise DrainError(f"graph/CAS task is not an owner-controlled regular file: {path}")
        with os.fdopen(os.dup(file_fd), "rb") as handle:
            data = handle.read(MAX_GRAPH_TASK_BYTES + 1)
        if len(data) > MAX_GRAPH_TASK_BYTES:
            raise DrainError(f"task-like Markdown record exceeds the graph/CAS byte bound: {path}")
        final_file_state = os.fstat(file_fd)
        bound_file = os.stat(relative.name, dir_fd=directory_fds[-1], follow_symlinks=False)
        stable_fields = (file_state.st_dev, file_state.st_ino, file_state.st_size, file_state.st_mtime_ns)
        if (
            (final_file_state.st_dev, final_file_state.st_ino, final_file_state.st_size, final_file_state.st_mtime_ns)
            != stable_fields
            or (bound_file.st_dev, bound_file.st_ino, bound_file.st_size, bound_file.st_mtime_ns) != stable_fields
            or len(data) != file_state.st_size
        ):
            raise DrainError(f"graph/CAS task path identity drifted during capture: {path}")
        parents: list[DirectoryIdentity] = []
        for index, (directory_fd, name) in enumerate(zip(directory_fds, directory_names, strict=True)):
            state = os.fstat(directory_fd)
            if not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid():
                raise DrainError(f"graph/CAS task parent has unsafe identity: {path}")
            if index:
                entry = os.stat(relative.parts[index - 1], dir_fd=directory_fds[index - 1], follow_symlinks=False)
                if (entry.st_dev, entry.st_ino) != (state.st_dev, state.st_ino):
                    raise DrainError(f"graph/CAS task parent identity drifted during capture: {path}")
            parents.append(DirectoryIdentity(name, state.st_dev, state.st_ino, state.st_uid, stat.S_IMODE(state.st_mode)))
        row = TaskRecordRow(
            relative.as_posix(),
            file_state.st_dev,
            file_state.st_ino,
            file_state.st_uid,
            stat.S_IMODE(file_state.st_mode),
            len(data),
            sha256_bytes(data),
            tuple(parents),
        )
        return data, row
    except OSError as error:
        raise DrainError(f"cannot capture no-follow graph/CAS identity for {path}: {error}") from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def enumerate_task_record_paths(root: Path) -> tuple[Path, ...]:
    records: list[Path] = []
    for path in sorted(root.rglob("*.md"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if "manager_mail" in relative.parts:
            continue
        try:
            data = task_bytes_no_follow(root, path)
        except (DrainError, OSError) as error:
            raise DrainError(f"cannot enumerate possible task record without following links: {path}") from error
        if graph_record_metadata(data, root, path) is not None:
            records.append(path.resolve())
    return tuple(records)


def capture_active_graph(root: Path, expected_paths: tuple[Path, ...]) -> ActiveGraphSnapshot:
    def capture_once() -> ActiveGraphSnapshot:
        records: list[TaskRecordRow] = []
        active: list[ActiveGraphRow] = []
        for path in expected_paths:
            data, record = nofollow_record_snapshot(root, path)
            metadata = graph_record_metadata(data, root, path)
            if metadata is None:
                raise DrainError(f"bound task record no longer has authoritative task metadata: {path}")
            records.append(record)
            if metadata.status in ACTIVE_TASK_STATUSES:
                active.append(
                    ActiveGraphRow(
                        record.path,
                        graph_target(metadata.runat),
                        graph_target(metadata.managerat),
                        metadata.is_manager,
                    )
                )
        return ActiveGraphSnapshot(tuple(records), tuple(active))

    first = capture_once()
    second = capture_once()
    if first != second:
        raise DrainError("complete graph/CAS task identities drifted during capture")
    return first


def capture_locked_active_graph(root: Path, expected_paths: tuple[Path, ...]) -> ActiveGraphSnapshot:
    if enumerate_task_record_paths(root) != expected_paths:
        raise DrainError("task-record membership drifted before graph/CAS capture")
    snapshot = capture_active_graph(root, expected_paths)
    if enumerate_task_record_paths(root) != expected_paths:
        raise DrainError("task-record membership drifted during graph/CAS capture")
    return snapshot


def lock_complete_task_records(root: Path, locks: ExitStack, extra_paths: set[Path]) -> tuple[Path, ...]:
    initial = enumerate_task_record_paths(root)
    lock_paths = sorted({*initial, *extra_paths}, key=lambda candidate: candidate.relative_to(root).as_posix())
    for path in lock_paths:
        locks.enter_context(task_file_lock(path))
    current = enumerate_task_record_paths(root)
    if current != initial:
        raise DrainError("task-record membership changed while complete graph locks were acquired")
    return current


def graph_direct_counts(rows: tuple[ActiveGraphRow, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.managerat] = counts.get(row.managerat, 0) + 1
    return dict(sorted(counts.items()))


def require_acyclic_graph(rows: tuple[ActiveGraphRow, ...]) -> None:
    edges: dict[str, set[str]] = {}
    nodes: set[str] = set()
    for row in rows:
        nodes.add(row.managerat)
        if row.runat == "retired":
            continue
        nodes.add(row.runat)
        edges.setdefault(row.managerat, set()).add(row.runat)
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> None:
        if node in visiting:
            start = stack.index(node)
            raise DrainError(f"post-correction active ownership graph is cyclic: {' -> '.join([*stack[start:], node])}")
        if node in visited:
            return
        visiting.add(node)
        stack.append(node)
        for child in sorted(edges.get(node, ())):
            visit(child)
        stack.pop()
        visiting.remove(node)
        visited.add(node)

    for node in sorted(nodes):
        visit(node)


def graph_projection_value(rows: tuple[ActiveGraphRow, ...]) -> dict[str, object]:
    values = [asdict(row) for row in rows]
    return {
        "tasks": values,
        "task_count": len(values),
        "sha256": sha256_bytes(canonical_json(values)),
        "direct_counts": graph_direct_counts(rows),
    }


def unslop_graph_cas_value(snapshot: ActiveGraphSnapshot, task_data: bytes, task_replacement: bytes, root: Path) -> dict[str, object]:
    task_records = [record for record in snapshot.records if record.path == UNSLOP_TASK]
    task_rows = [row for row in snapshot.active_rows if row.path == UNSLOP_TASK]
    if len(task_records) != 1 or len(task_rows) != 1:
        raise DrainError("complete graph/CAS does not contain one active Unslop task")
    task_record = task_records[0]
    task_row = task_rows[0]
    if (
        task_record.sha256 != sha256_bytes(task_data)
        or task_record.size != len(task_data)
        or task_row.runat != graph_target(UNSLOP_TARGET)
        or task_row.managerat != graph_target(UNSLOP_MANAGER)
        or task_row.is_manager
    ):
        raise DrainError("complete graph/CAS does not bind the exact Unslop source task")
    post_rows = tuple(row for row in snapshot.active_rows if row.path != UNSLOP_TASK)
    pre_counts = graph_direct_counts(snapshot.active_rows)
    post_counts = graph_direct_counts(post_rows)
    changed_managers = sorted(manager for manager in set(pre_counts) | set(post_counts) if pre_counts.get(manager, 0) != post_counts.get(manager, 0))
    expected_manager = graph_target(UNSLOP_MANAGER)
    if changed_managers != [expected_manager]:
        raise DrainError("status-only Unslop simulation changes an unauthorized manager set")
    projected_count = post_counts.get(expected_manager, 0)
    if projected_count > MANAGER_DIRECT_TASK_LIMIT:
        raise DrainError(
            f"status-only Unslop simulation leaves {UNSLOP_MANAGER} with {projected_count} direct active tasks; one additional independently authorized acyclic capacity correction is required"
        )
    require_acyclic_graph(post_rows)
    records = [asdict(record) for record in snapshot.records]
    return {
        "schema": UNSLOP_GRAPH_CAS_SCHEMA,
        "root": str(root),
        "operation": "status-only-unslop-done",
        "write_set": [
            {
                "path": UNSLOP_TASK,
                "before_sha256": sha256_bytes(task_data),
                "before_size": len(task_data),
                "after_sha256": sha256_bytes(task_replacement),
                "after_size": len(task_replacement),
            }
        ],
        "record_generation": {
            "records": records,
            "record_count": len(records),
            "paths_sha256": snapshot.lock_generation_sha256,
            "sha256": sha256_bytes(canonical_json(records)),
        },
        "pre": graph_projection_value(snapshot.active_rows),
        "post": graph_projection_value(post_rows),
        "scoped_managers": [expected_manager],
        "post_graph_acyclic": True,
        "unrelated_record_policy": "exact-bytes-and-path-identity",
    }


def load_unslop_graph_cas(path: Path, expected_sha256: str) -> dict[str, object]:
    data = private_file_bytes(path, expected_sha256, "Unslop graph/CAS")
    if len(data) > 64_000_000:
        raise DrainError("Unslop graph/CAS exceeds its byte bound")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("Unslop graph/CAS is not valid JSON") from error
    if not isinstance(value, dict) or value.get("schema") != UNSLOP_GRAPH_CAS_SCHEMA:
        raise DrainError("Unslop graph/CAS has an invalid schema")
    return value


def validate_unslop_graph_cas_pre(
    root: Path,
    graph_cas: dict[str, object],
    snapshot: ActiveGraphSnapshot,
    task_data: bytes,
    task_replacement: bytes,
) -> None:
    expected = unslop_graph_cas_value(snapshot, task_data, task_replacement, root)
    if canonical_json(graph_cas) != canonical_json(expected):
        raise DrainError("complete Unslop graph/CAS input drifted from the locked pre-state")


def validate_unslop_graph_cas_post(
    root: Path,
    graph_cas: dict[str, object],
    snapshot: ActiveGraphSnapshot,
    task_replacement: bytes,
) -> None:
    raw_generation = graph_cas.get("record_generation")
    raw_post = graph_cas.get("post")
    if not isinstance(raw_generation, dict) or not isinstance(raw_generation.get("records"), list) or not isinstance(raw_post, dict):
        raise DrainError("Unslop graph/CAS lacks its bound post-state")
    try:
        before_records = {str(value["path"]): value for value in raw_generation["records"] if isinstance(value, dict)}
    except (KeyError, TypeError) as error:
        raise DrainError("Unslop graph/CAS record generation is invalid") from error
    after_records = {record.path: record for record in snapshot.records}
    if set(after_records) != set(before_records) or len(before_records) != len(raw_generation["records"]):
        raise DrainError("task-record membership drifted during Unslop reconciliation")
    for path, current in after_records.items():
        before = before_records[path]
        if path != UNSLOP_TASK:
            if canonical_json(asdict(current)) != canonical_json(before):
                raise DrainError(f"unrelated graph/CAS task identity or bytes drifted: {path}")
            continue
        expected_static = {
            "path": before.get("path"),
            "device": before.get("device"),
            "uid": before.get("uid"),
            "mode": before.get("mode"),
            "parents": before.get("parents"),
        }
        current_static = {
            "path": current.path,
            "device": current.device,
            "uid": current.uid,
            "mode": current.mode,
            "parents": [asdict(parent) for parent in current.parents],
        }
        if (
            current_static != expected_static
            or current.sha256 != sha256_bytes(task_replacement)
            or current.size != len(task_replacement)
        ):
            raise DrainError("Unslop replacement path identity or exact bytes drifted")
    if canonical_json(graph_projection_value(snapshot.active_rows)) != canonical_json(raw_post):
        raise DrainError("complete active membership graph drifted from the simulated post-state")
    require_acyclic_graph(snapshot.active_rows)
    counts = graph_direct_counts(snapshot.active_rows)
    scoped_managers = graph_cas.get("scoped_managers")
    if not isinstance(scoped_managers, list):
        raise DrainError("Unslop graph/CAS lacks its scoped manager set")
    for manager in scoped_managers:
        if not isinstance(manager, str) or counts.get(manager, 0) > MANAGER_DIRECT_TASK_LIMIT:
            raise DrainError("post-write scoped manager exceeds the direct-task limit")


def validate_unslop_graph_cas_rollback(
    root: Path,
    before: ActiveGraphSnapshot,
    after_rollback: ActiveGraphSnapshot,
    task_data: bytes,
) -> None:
    before_records = {record.path: record for record in before.records}
    after_records = {record.path: record for record in after_rollback.records}
    if set(before_records) != set(after_records) or before.active_rows != after_rollback.active_rows:
        raise DrainError("graph/CAS membership or active projection was not restored after rollback")
    for path, current in after_records.items():
        original = before_records[path]
        if path != UNSLOP_TASK:
            if current != original:
                raise DrainError(f"unrelated graph/CAS task drifted during rollback: {path}")
            continue
        if (
            current.path != original.path
            or current.device != original.device
            or current.uid != original.uid
            or current.mode != original.mode
            or current.parents != original.parents
            or current.size != len(task_data)
            or current.sha256 != sha256_bytes(task_data)
        ):
            raise DrainError("Unslop source bytes or path identity were not restored after rollback")


def validate_unslop_authority(root: Path) -> dict[str, str]:
    source_name = UNSLOP_AUTHORITY_SOURCE
    source_lines = UNSLOP_AUTHORITY_LINES
    source_sha256 = UNSLOP_AUTHORITY_SOURCE_SHA256
    envelope_name = UNSLOP_AUTHORITY_ENVELOPE
    envelope_sha256 = UNSLOP_AUTHORITY_ENVELOPE_SHA256
    if source_name is None or source_lines is None or source_sha256 is None or envelope_name is None or envelope_sha256 is None:
        raise DrainError("Unslop execution authority is not configured from an exact Human-instruction injection")
    source = Path(source_name)
    envelope = Path(envelope_name)
    args = TaskStatusArgs(
        root=root,
        task_file=root / UNSLOP_TASK,
        status="done",
        blocked_on="",
        authority_file=source,
        authority_lines=source_lines,
        authority_sha256=source_sha256,
        authority_envelope=envelope,
        authority_envelope_sha256=envelope_sha256,
    )
    try:
        excerpt, locator = read_park_authority(args)
        envelope_ref = read_park_authority_envelope(args, excerpt, locator)
    except (OSError, ValueError) as error:
        raise DrainError(f"Unslop Human authority provenance is invalid: {error}") from error
    if excerpt.replace("\r\n", "\n").rstrip("\n") != UNSLOP_AUTHORITY_TEXT:
        raise DrainError("authenticated Human-instruction excerpt does not contain the exact Unslop instruction")
    return {
        "source": locator,
        "source_sha256": source_sha256,
        "envelope": envelope_ref,
        "envelope_sha256": envelope_sha256,
        "instruction": UNSLOP_AUTHORITY_TEXT,
    }


def require_no_unslop_todo_row(root: Path, task: Path, todo_data: bytes) -> None:
    try:
        todo_text = todo_data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DrainError("Unslop TODO is not UTF-8") from error
    if any(task in todo_row_task_paths(root, line) for line in todo_text.splitlines()):
        raise DrainError("Unslop no-mail reconciliation requires no TODO row")


def active_manager_owner(root: Path, target: str) -> tuple[Path, bytes]:
    matches: list[tuple[Path, bytes]] = []
    for candidate in sorted(root.rglob("*.md")):
        if "manager_mail" in candidate.parts:
            continue
        try:
            data, record = nofollow_record_snapshot(root, candidate)
        except (DrainError, OSError) as error:
            raise DrainError(f"unsafe or drifting record prevents complete manager-owner proof: {candidate}") from error
        try:
            metadata = parse_task_metadata(data.decode("utf-8"), root)
        except (UnicodeDecodeError, TaskFrontmatterError) as error:
            decoded = data.decode("utf-8", errors="ignore")
            if raw_runat_claims_namespace(decoded, target):
                raise DrainError(f"invalid record conflicts with manager owner: {candidate}") from error
            continue
        if metadata is None or metadata.status == "done" or not metadata.is_manager:
            continue
        try:
            same_target = canonical_target(metadata.runat) == canonical_target(target)
        except DrainError:
            same_target = False
        if same_target:
            matches.append((root / record.path, data))
    if len(matches) != 1:
        raise DrainError(f"expected exactly one active manager record for {target}, found {len(matches)}")
    return matches[0]


def completed_task_replacements(
    root: Path,
    task: Path,
    text: str,
    todo_text: str,
    created_at: datetime,
    exact_close_note: str = "",
) -> tuple[str, str]:
    metadata = parse_task_metadata(text, root)
    if (
        metadata is None
        or metadata.status != "blocked"
        or canonical_target(metadata.runat) != COMPLETED_SHELL_TARGET
        or metadata.blocked_on != COMPLETED_SHELL_BLOCKER
        or metadata.is_manager
        or metadata.pending_task_items
        or has_pending_marker(text)
        or text.count(COMPLETED_SHELL_MESSAGE_ID) != 1
    ):
        raise DrainError("completed-shell task does not match the exact queue-empty completion state")
    if metadata.managerat.startswith("h"):
        raise DrainError("completed-shell task has a Human-owned manager")
    todo_replacement = reconcile_todo_text(root, task, todo_text, metadata.runat, "previous", ("current",))
    note = exact_close_note or close_note(metadata.runat, "", created_at)
    note_pattern = re.compile(
        rf"\n\(manager closed Codex agent \d{{2}}-\d{{2}} \d{{2}}:\d{{2}} [A-Za-z0-9_+\-:]+; tmux target `{re.escape(metadata.runat)}`; Codex session id not found in captured tmux output\.\)\n"
    )
    if note_pattern.fullmatch(note) is None:
        raise DrainError("completed-shell close note is not the supported exact lifecycle note")
    task_replacement = update_frontmatter_status(text + note, "done", "", root)
    return task_replacement, todo_replacement


def validate_completion_receipt(
    root: Path,
    task: Path,
    text: str,
    receipt: Path,
    receipt_sha256: str,
) -> tuple[dict[str, str], bytes]:
    plan = build_completion_email(root, task, text, "task done")
    expected = completion_email_state_dir().resolve() / "completion-email-delivered" / (plan.key if plan is not None else "invalid")
    if plan is None or receipt.resolve() != expected:
        raise DrainError("completion receipt is not the canonical message receipt")
    data = private_file_bytes(receipt, receipt_sha256, "completion receipt")
    if data != f"{plan.target}\t{task.name}\n".encode():
        raise DrainError("completion receipt content does not authenticate the canonical message")
    return {"path": str(receipt), "sha256": receipt_sha256, "message_id": COMPLETED_SHELL_MESSAGE_ID, "message_key": plan.key}, data


def write_binding(output: Path, root: Path, value: dict[str, object], protected: set[Path]) -> dict[str, object]:
    validate_new_private_output(output, root, protected)
    data = canonical_json(value)
    write_new_private_output(output, data)
    return {"path": str(output.resolve()), "sha256": sha256_bytes(data), "value": value}


def validate_unslop_output(path: Path, root: Path, protected: set[Path]) -> None:
    validate_new_private_output(path, root, protected)
    if path.resolve(strict=False).is_relative_to(UNSLOP_RESULT_ROOT.resolve()):
        raise DrainError("Unslop output must be outside the result repository")


def bind_taskless_shell(
    root: Path,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    executor_pane_id = authenticate_executor(executor)
    review = validate_code_review(root, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, executor)
    target = TASKLESS_SHELL_TARGET
    with root_membership_lock(root), task_target_lock(root, target):
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted during taskless-shell binding")
        authority = namespace_authority(root)
        matches = task_records_for_target(root, target)
        if matches:
            raise DrainError(f"taskless shell has task records: {[str(path.relative_to(root)) for path in matches]}")
        snapshot = shell_snapshot(target, require_empty_transcript=True)
        if namespace_authority(root) != authority:
            raise DrainError("Source-1261 authority drifted during taskless-shell binding")
        value: dict[str, object] = {
            "schema": SHELL_BINDING_SCHEMA,
            "kind": "taskless-empty-shell",
            "target": target,
            "task_record_count": 0,
            "shell": asdict(snapshot),
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "executor_pane_id": executor_pane_id,
            "human_authority": authority,
            "email_policy": "suppressed",
        }
        return write_binding(output.resolve(), root, value, {review_path.resolve(), consumed_receipt.resolve(), *root.rglob("*.md")})


def bind_completed_shell(
    root: Path,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    completion_receipt: Path,
    completion_receipt_sha256: str,
    output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    executor_pane_id = authenticate_executor(executor)
    review = validate_code_review(root, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, executor)
    task = root / COMPLETED_SHELL_TASK
    todo = root / "TODO.md"
    with root_membership_lock(root), task_target_lock(root, COMPLETED_SHELL_TARGET), ExitStack() as locks:
        authority = namespace_authority(root)
        initial_task_data = task_bytes_no_follow(root, task)
        try:
            initial_metadata = parse_task_metadata(initial_task_data.decode("utf-8"), root)
        except (UnicodeDecodeError, TaskFrontmatterError) as error:
            raise DrainError("completed-shell task metadata is invalid") from error
        if initial_metadata is None:
            raise DrainError("completed-shell task metadata is missing")
        initial_owner_path, _ = active_manager_owner(root, initial_metadata.managerat)
        for locked_path in sorted((task, todo, initial_owner_path), key=str):
            locks.enter_context(task_file_lock(locked_path))
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted during completed-shell binding")
        task_data = task_bytes_no_follow(root, task)
        todo_data = task_bytes_no_follow(root, todo)
        try:
            text = task_data.decode("utf-8")
            todo_text = todo_data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DrainError("completed-shell task or TODO is not UTF-8") from error
        created_at = datetime.now().astimezone()
        metadata = parse_task_metadata(text, root)
        assert metadata is not None
        bound_close_note = close_note(metadata.runat, "", created_at)
        task_replacement, todo_replacement = completed_task_replacements(root, task, text, todo_text, created_at, bound_close_note)
        active_paths = task_records_for_target(root, metadata.runat, active_only=True)
        if active_paths != (task.resolve(),):
            raise DrainError("completed shell does not have exactly one authoritative active task")
        owner_path, owner_data = active_manager_owner(root, metadata.managerat)
        if owner_path != initial_owner_path:
            raise DrainError("completed-shell manager record changed during binding")
        owner_pane_id = exact_pane_id(metadata.managerat)
        if not owner_pane_id:
            raise DrainError("completed-shell task manager is not live")
        receipt, _ = validate_completion_receipt(root, task, text, completion_receipt, completion_receipt_sha256)
        snapshot = shell_snapshot(COMPLETED_SHELL_TARGET)
        if snapshot.pane_id != COMPLETED_SHELL_PANE:
            raise DrainError("completed shell is not the exact authorized pane")
        mutable = (task, todo)
        dirty = repository_dirty_snapshot(root, mutable)
        if namespace_authority(root) != authority:
            raise DrainError("Source-1261 authority drifted during completed-shell binding")
        value: dict[str, object] = {
            "schema": SHELL_BINDING_SCHEMA,
            "kind": "completed-task-shell",
            "target": COMPLETED_SHELL_TARGET,
            "task": {
                "path": COMPLETED_SHELL_TASK,
                "sha256": sha256_bytes(task_data),
                "source_base64": base64.b64encode(task_data).decode("ascii"),
                "replacement_base64": base64.b64encode(task_replacement.encode()).decode("ascii"),
            },
            "todo": {
                "path": "TODO.md",
                "sha256": sha256_bytes(todo_data),
                "source_base64": base64.b64encode(todo_data).decode("ascii"),
                "replacement_base64": base64.b64encode(todo_replacement.encode()).decode("ascii"),
            },
            "manager": {
                "target": metadata.managerat,
                "task": str(owner_path.relative_to(root)),
                "task_sha256": sha256_bytes(owner_data),
                "pane_id": owner_pane_id,
            },
            "completion_receipt": receipt,
            "shell": asdict(snapshot),
            "dirty_manifest": dirty,
            "prepared_at": created_at.isoformat(),
            "close_note": bound_close_note,
            "human_authority": authority,
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "executor_pane_id": executor_pane_id,
            "email_policy": "suppressed",
        }
        protected = {task, todo, completion_receipt.resolve(), review_path.resolve(), consumed_receipt.resolve(), *root.rglob("*.md")}
        return write_binding(output.resolve(), root, value, protected)


def bind_unslop_done(
    root: Path,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    graph_cas_path: Path,
    graph_cas_sha256: str,
    output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    executor_pane_id = authenticate_executor(executor)
    review = validate_code_review(root, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, executor)
    authority = validate_unslop_authority(root)
    graph_cas_path = graph_cas_path.resolve()
    if graph_cas_path.is_relative_to(root) or graph_cas_path.is_relative_to(UNSLOP_RESULT_ROOT.resolve()):
        raise DrainError("Unslop graph/CAS must be disjoint from both protected repositories")
    graph_cas = load_unslop_graph_cas(graph_cas_path, graph_cas_sha256)
    task = root / UNSLOP_TASK
    todo = root / "TODO.md"
    with root_membership_lock(root), task_target_lock(root, UNSLOP_TARGET), ExitStack() as locks:
        graph_paths = lock_complete_task_records(root, locks, {todo})
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted during Unslop binding")
        task_data = task_bytes_no_follow(root, task)
        todo_data = task_bytes_no_follow(root, todo)
        task_replacement = unslop_task_replacement(root, task_data)
        graph_snapshot = capture_locked_active_graph(root, graph_paths)
        validate_unslop_graph_cas_pre(root, graph_cas, graph_snapshot, task_data, task_replacement)
        require_no_unslop_todo_row(root, task, todo_data)
        metadata = parse_task_metadata(task_data.decode("utf-8"), root)
        assert metadata is not None
        shared_manager_path, current_shared_data = active_manager_owner(root, UNSLOP_TARGET)
        custody_manager_path, current_custody_data = active_manager_owner(root, UNSLOP_MANAGER)
        active_paths = set(task_records_for_target(root, UNSLOP_TARGET, active_only=True))
        if active_paths != {task.resolve(), shared_manager_path}:
            raise DrainError("Unslop shared target has unexpected active task ownership")
        pane = shared_pane_snapshot()
        result = unslop_result_snapshot()
        dirty = repository_dirty_snapshot(root, (task,))
        if validate_unslop_authority(root) != authority:
            raise DrainError("Unslop Human authority drifted during binding")
        if load_unslop_graph_cas(graph_cas_path, graph_cas_sha256) != graph_cas:
            raise DrainError("Unslop graph/CAS file drifted during binding")
        if capture_locked_active_graph(root, graph_paths) != graph_snapshot:
            raise DrainError("complete graph/CAS drifted during Unslop binding")
        graph_post = graph_cas.get("post")
        if not isinstance(graph_post, dict) or not isinstance(graph_post.get("sha256"), str):
            raise DrainError("Unslop graph/CAS lacks an exact post-projection digest")
        value: dict[str, object] = {
            "schema": UNSLOP_BINDING_SCHEMA,
            "kind": "unslop-shared-pane-done",
            "task": {
                "path": UNSLOP_TASK,
                "sha256": sha256_bytes(task_data),
                "source_base64": base64.b64encode(task_data).decode("ascii"),
                "replacement_base64": base64.b64encode(task_replacement).decode("ascii"),
            },
            "todo": {
                "path": "TODO.md",
                "sha256": sha256_bytes(todo_data),
                "source_base64": base64.b64encode(todo_data).decode("ascii"),
                "replacement_base64": base64.b64encode(todo_data).decode("ascii"),
            },
            "shared_manager": {
                "target": UNSLOP_TARGET,
                "task": str(shared_manager_path.relative_to(root)),
                "task_sha256": sha256_bytes(current_shared_data),
            },
            "custody_manager": {
                "target": metadata.managerat,
                "task": str(custody_manager_path.relative_to(root)),
                "task_sha256": sha256_bytes(current_custody_data),
            },
            "shared_pane": asdict(pane),
            "accepted_completion_evidence": UNSLOP_ACCEPTED_EVIDENCE,
            "result": result,
            "work_log_dirty_manifest": dirty,
            "graph_cas": {
                "path": str(graph_cas_path),
                "sha256": graph_cas_sha256,
                "lock_generation_sha256": graph_snapshot.lock_generation_sha256,
                "pre_projection_sha256": graph_projection_value(graph_snapshot.active_rows)["sha256"],
                "post_projection_sha256": graph_post["sha256"],
            },
            "human_authority": authority,
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "executor_pane_id": executor_pane_id,
            "email_policy": "suppressed",
            "pending_policy": "unchanged",
            "todo_policy": "unchanged",
            "pane_policy": "identity-only",
        }
        protected = {
            (root / str(authority["source"]).partition(":")[0]).resolve(),
            (root / str(authority["envelope"])).resolve(),
            task,
            todo,
            shared_manager_path,
            custody_manager_path,
            review_path.resolve(),
            consumed_receipt.resolve(),
            graph_cas_path,
            *root.rglob("*.md"),
        }
        validate_unslop_output(output.resolve(), root, protected)
        data = canonical_json(value)
        write_new_private_output(output.resolve(), data)
        return {"path": str(output.resolve()), "sha256": sha256_bytes(data), "value": value}


def minimal_target_text(target: str) -> str:
    canonical = canonical_target(target)
    if canonical.endswith(".0"):
        return canonical.removesuffix(".0")
    return canonical


def require_minimal_target_text(target: str, label: str) -> str:
    if target.startswith("h"):
        raise DrainError(f"{label} must not be a Human-owned target")
    if target != minimal_target_text(target):
        raise DrainError(f"{label} must use the canonical non-aliased target spelling")
    return canonical_target(target)


def require_absent_history_target(target: str) -> dict[str, object]:
    canonical = require_minimal_target_text(target, "historical manager target")
    raw = tmux_bytes(
        ["list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}|#{pane_id}|#{pane_pid}|#{pane_current_command}"],
        f"absence proof for {target}",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DrainError(f"tmux inventory is not UTF-8 while proving absence of {target}") from error
    matches: list[str] = []
    for line in text.splitlines():
        pane_target, separator, rest = line.partition("|")
        if not separator:
            raise DrainError("tmux inventory row is malformed")
        fields = rest.split("|")
        if len(fields) != 3 or re.fullmatch(r"%\d+", fields[0]) is None or not fields[1].isdigit():
            raise DrainError("tmux inventory identity row is malformed")
        try:
            if canonical_target(pane_target) == canonical:
                matches.append(line)
        except DrainError as error:
            raise DrainError("tmux inventory contains an invalid target row") from error
    if matches:
        raise DrainError(f"historical manager target is live or rebound: {target}")
    if inspect_target(target) != "absent":
        raise DrainError(f"historical manager target state is not exactly absent: {target}")
    return {
        "target": target,
        "canonical_target": canonical,
        "state": "absent",
        "pane_inventory_sha256": sha256_bytes(raw),
    }


def split_frontmatter_lines(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise DrainError("task file has no frontmatter")
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[: index + 1], lines[index + 1 :]
    raise DrainError("task frontmatter lacks a closing marker")


def absent_manager_history_task_replacement(root: Path, task: Path, data: bytes, target: str) -> bytes:
    try:
        text = data.decode("utf-8")
        metadata = parse_task_metadata(text, root)
    except (UnicodeDecodeError, TaskFrontmatterError) as error:
        raise DrainError("absent-manager task metadata is invalid") from error
    if metadata is None:
        raise DrainError("absent-manager task metadata is missing")
    require_minimal_target_text(target, "historical manager target")
    if (
        metadata.version != "v1.0.0"
        or metadata.status != "long_running"
        or metadata.runat != target
        or metadata.tool != "codex"
        or not metadata.is_manager
        or metadata.managerat.startswith("h")
        or not metadata.blocked_on
        or has_pending_marker(text)
    ):
        raise DrainError("absent-manager history requires one v1 long_running Codex manager with an existing blocker and no pending body marker")
    if metadata.pending_task_items != tuple(metadata.pending_task_items):
        raise DrainError("absent-manager queue is malformed")
    frontmatter, body = split_frontmatter_lines(text)
    status_updates = 0
    runat_updates = 0
    blocked_on_seen = 0
    replacement_frontmatter: list[str] = []
    for line in frontmatter:
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        if stripped == "status: long_running":
            replacement_frontmatter.append(f"status: blocked{ending}")
            status_updates += 1
        elif stripped == f"runat: {target}":
            replacement_frontmatter.append(f"runat: {ABSENT_MANAGER_HISTORICAL_TARGET}{ending}")
            runat_updates += 1
        else:
            if stripped.startswith("blocked_on: "):
                blocked_on_seen += 1
            replacement_frontmatter.append(line)
    if (status_updates, runat_updates, blocked_on_seen) != (1, 1, 1):
        raise DrainError("absent-manager history requires exact status, runat, and blocker frontmatter lines")
    replacement = "".join([*replacement_frontmatter, *body]).encode()
    try:
        replacement_metadata = parse_task_metadata(replacement.decode("utf-8"), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as error:
        raise DrainError("absent-manager replacement metadata is invalid") from error
    if (
        replacement_metadata is None
        or replacement_metadata.status != "blocked"
        or replacement_metadata.runat != ABSENT_MANAGER_HISTORICAL_TARGET
        or replacement_metadata.managerat != metadata.managerat
        or replacement_metadata.is_manager is not True
        or replacement_metadata.blocked_on != metadata.blocked_on
        or replacement_metadata.pending_task_items != metadata.pending_task_items
    ):
        raise DrainError("absent-manager replacement does not preserve custody metadata")
    if not str(task.relative_to(root)).endswith(".md"):
        raise DrainError("absent-manager task path is invalid")
    return replacement


def absent_manager_history_todo_replacement(root: Path, task: Path, todo_data: bytes, target: str) -> bytes:
    try:
        text = todo_data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DrainError("absent-manager TODO is not UTF-8") from error
    relative = task.relative_to(root).as_posix()
    lines = text.splitlines(keepends=True)
    section = ""
    rows: list[tuple[int, str, str]] = []
    current_headers = 0
    for index, line in enumerate(lines):
        heading = line.strip()
        if heading.endswith(":"):
            section = heading[:-1].casefold()
            if section == "current":
                current_headers += 1
            continue
        if task in todo_row_task_paths(root, line):
            rows.append((index, section, line))
    if current_headers != 1 or len(rows) != 1 or rows[0][1] != "current":
        raise DrainError("absent-manager history requires exactly one current TODO row")
    row_index, _, row = rows[0]
    if row.rstrip("\r\n") != f"{relative} {target}":
        raise DrainError("absent-manager TODO row does not name the exact source target")
    ending = row[len(row.rstrip("\r\n")) :]
    lines[row_index] = f"{relative}{ending}"
    updated = "".join(lines)
    updated_rows = [line for line in updated.splitlines() if task in todo_row_task_paths(root, line)]
    if updated_rows != [relative]:
        raise DrainError("absent-manager targetless TODO row is not canonical")
    return updated.encode()


def require_absent_manager_source_state(
    root: Path,
    task: Path,
    task_data: bytes,
    todo_data: bytes,
    target: str,
    task_sha256: str,
) -> tuple[bytes, bytes, dict[str, object], dict[str, object], dict[str, object]]:
    if sha256_bytes(task_data) != task_sha256:
        raise DrainError("absent-manager task digest mismatch")
    target_absence = require_absent_history_target(target)
    metadata = parse_task_metadata(task_data.decode("utf-8"), root)
    if metadata is None:
        raise DrainError("absent-manager task metadata is missing")
    if metadata.runat != target:
        raise DrainError("absent-manager task target drifted")
    active_paths = task_records_for_target(root, target, active_only=True)
    if active_paths != (task.resolve(),):
        raise DrainError("absent-manager source target does not have singular active ownership")
    owner_path, owner_data = active_manager_owner(root, target)
    if owner_path != task.resolve() or owner_data != task_data:
        raise DrainError("absent-manager active manager ownership drifted")
    task_replacement = absent_manager_history_task_replacement(root, task, task_data, target)
    todo_replacement = absent_manager_history_todo_replacement(root, task, todo_data, target)
    _, owner_generation = nofollow_record_snapshot(root, task)
    owner_generation_value = json.loads(canonical_json(asdict(owner_generation)))
    return (
        task_replacement,
        todo_replacement,
        target_absence,
        owner_generation_value,
        {
            "status": metadata.status,
            "runat": metadata.runat,
            "managerat": metadata.managerat,
            "is_manager": metadata.is_manager,
            "blocked_on": metadata.blocked_on,
            "pending_task_items": list(metadata.pending_task_items),
            "session_id": metadata.session_id,
        },
    )


def bind_absent_manager_history(
    root: Path,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    output: Path,
    executor: str,
    task_name: str,
    task_sha256: str,
    target: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    executor_pane_id = authenticate_executor(executor)
    review = validate_code_review(root, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, executor)
    task = (root / task_name).resolve()
    todo = root / "TODO.md"
    if not task.is_relative_to(root) or task.name != task_name or task == todo:
        raise DrainError("absent-manager task name must be one root-local Markdown record")
    with root_membership_lock(root), task_target_lock(root, target), ExitStack() as locks:
        for locked_path in sorted({task, todo}, key=str):
            locks.enter_context(task_file_lock(locked_path))
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted during absent-manager binding")
        authority = namespace_authority(root)
        task_data = task_bytes_no_follow(root, task)
        todo_data = task_bytes_no_follow(root, todo)
        task_replacement, todo_replacement, absence, owner_generation, metadata = require_absent_manager_source_state(
            root,
            task,
            task_data,
            todo_data,
            target,
            task_sha256,
        )
        if namespace_authority(root) != authority:
            raise DrainError("Source-1261 authority drifted during absent-manager binding")
        dirty = repository_dirty_snapshot(root, (task, todo))
        value: dict[str, object] = {
            "schema": ABSENT_MANAGER_BINDING_SCHEMA,
            "kind": "absent-long-running-manager-history",
            "target": target,
            "task": {
                "path": task_name,
                "sha256": sha256_bytes(task_data),
                "source_base64": base64.b64encode(task_data).decode("ascii"),
                "replacement_base64": base64.b64encode(task_replacement).decode("ascii"),
            },
            "todo": {
                "path": "TODO.md",
                "sha256": sha256_bytes(todo_data),
                "source_base64": base64.b64encode(todo_data).decode("ascii"),
                "replacement_base64": base64.b64encode(todo_replacement).decode("ascii"),
            },
            "target_absence": absence,
            "owner_generation": owner_generation,
            "metadata": metadata,
            "dirty_manifest": dirty,
            "human_authority": authority,
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "executor_pane_id": executor_pane_id,
            "email_policy": "suppressed",
            "pane_policy": "no-tmux-mutation",
            "pending_policy": "preserved",
            "todo_policy": "current-targetless-custody",
        }
        protected = {task, todo, review_path.resolve(), consumed_receipt.resolve(), TRUSTED_ROOT / "manager_mail" / AUTHORITY_NAME, *root.rglob("*.md")}
        return write_binding(output.resolve(), root, value, protected)


def source1385_authority(root: Path) -> dict[str, str]:
    path = root / "manager_mail" / SOURCE1385_AUTHORITY_NAME
    data = task_bytes_no_follow(root, path)
    if sha256_bytes(data) != SOURCE1385_AUTHORITY_SHA256:
        raise DrainError("Source-1386 authority digest mismatch")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DrainError("Source-1386 authority is not UTF-8") from error
    if SOURCE1385_AUTHORITY_TEXT not in text:
        raise DrainError("Source-1386 authority lacks the exact cedit transfer directive")
    return {"path": str(path), "sha256": SOURCE1385_AUTHORITY_SHA256, "directive": SOURCE1385_AUTHORITY_TEXT}


def validate_source1385_code_review(
    root: Path,
    review_path: Path,
    expected_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    executor: str,
) -> dict[str, object]:
    data = private_file_bytes(review_path, expected_sha256, "Source-1385 code review")
    try:
        review = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("Source-1385 code review is not JSON") from error
    if not isinstance(review, dict) or review.get("schema") != SOURCE1385_CODE_REVIEW_SCHEMA or review.get("verdict") != "PASS":
        raise DrainError("Source-1385 code review is not an exact PASS")
    current_hashes = (
        sha256_bytes(nofollow_absolute_file_bytes(Path(__file__).absolute(), "reviewed helper source")[0]),
        sha256_bytes(nofollow_absolute_file_bytes(TEST_PATH.absolute(), "reviewed test source")[0]),
        sha256_bytes(nofollow_absolute_file_bytes(DOC_PATH.absolute(), "reviewed documentation source")[0]),
    )
    if (review.get("helper_sha256"), review.get("tests_sha256"), review.get("documentation_sha256")) != current_hashes:
        raise DrainError("Source-1385 review does not bind the current helper, tests, and documentation")
    reviewer_task_value = review.get("reviewer_task")
    reviewer = review.get("reviewer")
    report_agent = review.get("report_agent")
    if not isinstance(reviewer_task_value, str) or Path(reviewer_task_value).is_absolute() or ".." in Path(reviewer_task_value).parts:
        raise DrainError("Source-1385 review has an unsafe reviewer task")
    reviewer_task = (root / reviewer_task_value).resolve()
    if not reviewer_task.is_relative_to(root):
        raise DrainError("Source-1385 reviewer task escapes the work-log root")
    reviewer_data = task_bytes_no_follow(root, reviewer_task)
    reviewer_metadata = parse_task_metadata(reviewer_data.decode("utf-8"), root)
    if (
        reviewer_metadata is None
        or reviewer_metadata.status not in ACTIVE_TASK_STATUSES
        or reviewer_metadata.runat != reviewer
        or review.get("reviewer_task_sha256") != sha256_bytes(reviewer_data)
        or not isinstance(reviewer, str)
        or reviewer == executor
        or reviewer.startswith(("h", "cedit"))
        or not isinstance(report_agent, str)
        or REPORT_AGENT_RE.fullmatch(report_agent) is None
    ):
        raise DrainError("Source-1385 reviewer is not an active independent non-cedit owner")
    consumed_data = private_file_bytes(consumed_receipt, consumed_receipt_sha256, "consumed Source-1385 review")
    if consumed_receipt.resolve().parent != TRUSTED_RECEIPT_DIR.resolve():
        raise DrainError("consumed Source-1385 review is outside the trusted report store")
    try:
        consumed = json.loads(consumed_data)
    except json.JSONDecodeError as error:
        raise DrainError("consumed Source-1385 review is not JSON") from error
    consumed_input = consumed.get("input") if isinstance(consumed, dict) else None
    unsigned = {key: value for key, value in consumed.items() if key != "attestation_id"} if isinstance(consumed, dict) else {}
    if (
        not isinstance(consumed, dict)
        or consumed.get("schema") != "omo-report-consumed-closure/v1"
        or consumed.get("accepted") is not False
        or consumed.get("terminal") is not True
        or not isinstance(consumed_input, dict)
        or consumed_input.get("file_sha256") != expected_sha256
        or consumed.get("attestation_id") != bound_receipt_id(unsigned)
    ):
        raise DrainError("Source-1385 review lacks exact consumed-report provenance")
    verify_consumed_report(review_path, reviewer, report_agent, str(consumed.get("status", "")), consumed_data, root)
    return review


def validate_source1385_successor_eligibility(data: bytes, expected_sha256: str, successor_target: str, successor_task: str) -> dict[str, object]:
    if sha256_bytes(data) != expected_sha256:
        raise DrainError("Source-1385 successor eligibility digest mismatch")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DrainError("Source-1385 successor eligibility is not JSON") from error
    if not isinstance(value, dict):
        raise DrainError("Source-1385 successor eligibility is not an exact accepted binding")
    successor_manager = value.get("successor_manager")
    manager_task = value.get("successor_manager_task")
    source_session_id = value.get("source_session_id")
    if (
        set(value) != {
            "schema",
            "accepted",
            "accepted_by",
            "successor_target",
            "successor_task",
            "successor_manager",
            "successor_manager_task",
            "successor_manager_task_sha256",
            "source_session_id",
            "authority_sha256",
        }
        or value.get("schema") != "omo-namespace-drain-source1385-successor-eligibility/v1"
        or value.get("successor_target") != successor_target
        or value.get("successor_task") != successor_task
        or value.get("accepted") is not True
        or value.get("accepted_by") != successor_manager
        or not isinstance(successor_manager, str)
        or successor_manager.startswith(("h", "cedit"))
        or TARGET_RE.fullmatch(successor_manager) is None
        or not isinstance(manager_task, str)
        or Path(manager_task).is_absolute()
        or ".." in Path(manager_task).parts
        or SHA256_RE.fullmatch(str(value.get("successor_manager_task_sha256", ""))) is None
        or not isinstance(source_session_id, str)
        or re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", source_session_id) is None
        or value.get("authority_sha256") != SOURCE1385_AUTHORITY_SHA256
    ):
        raise DrainError("Source-1385 successor eligibility is not an exact accepted binding")
    return value


def source1385_todo_row(root: Path, todo_data: bytes, task: Path, target: str) -> str:
    try:
        text = todo_data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DrainError("Source-1385 TODO is not UTF-8") from error
    relative = task.relative_to(root).as_posix()
    section = ""
    current_headers = 0
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(":"):
            section = stripped[:-1].casefold()
            if section == "current":
                current_headers += 1
            continue
        if task in todo_row_task_paths(root, line):
            rows.append((section, line))
    if current_headers != 1 or rows != [("current", f"{relative} {target}")]:
        raise DrainError("Source-1385 requires exactly one canonical current TODO row")
    return rows[0][1]


def source1385_todo_replacement(root: Path, todo_data: bytes, task: Path, successor: Path, successor_target: str) -> bytes:
    try:
        text = todo_data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DrainError("Source-1385 TODO is not UTF-8") from error
    old_row = source1385_todo_row(root, todo_data, task, SOURCE1385_TARGET)
    successor_rows = [line for line in text.splitlines() if successor in todo_row_task_paths(root, line)]
    if successor_rows:
        raise DrainError("Source-1385 TODO already contains the successor")
    replacement = f"{successor.relative_to(root).as_posix()} {successor_target}"
    lines = text.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == old_row]
    if len(matches) != 1:
        raise DrainError("Source-1385 TODO row changed during replacement")
    index = matches[0]
    ending = lines[index][len(lines[index].rstrip("\r\n")) :]
    lines[index] = replacement + ending
    return "".join(lines).encode()


def source1385_replace_frontmatter(text: str, values: dict[str, str], removed: frozenset[str]) -> str:
    frontmatter, body = split_frontmatter_lines(text)
    changed = {key: 0 for key in values}
    result: list[str] = []
    for line in frontmatter:
        key, separator, current = line.rstrip("\r\n").partition(":")
        if separator and key in removed:
            continue
        if separator and key in values:
            ending = line[len(line.rstrip("\r\n")) :]
            result.append(f"{key}: {values[key]}{ending}")
            changed[key] += 1
        else:
            result.append(line)
    if any(count != 1 for count in changed.values()):
        raise DrainError("Source-1385 task lacks one exact transferable frontmatter field")
    return "".join([*result, *body])


def source1385_task_replacements(
    root: Path,
    task: Path,
    task_data: bytes,
    successor_task: str,
    successor_target: str,
    successor_manager: str,
) -> tuple[bytes, bytes]:
    try:
        text = task_data.decode("utf-8")
        metadata = parse_task_metadata(text, root)
    except (UnicodeDecodeError, TaskFrontmatterError) as error:
        raise DrainError("Source-1385 task metadata is invalid") from error
    if metadata is None:
        raise DrainError("Source-1385 task metadata is missing")
    if (
        task.name != SOURCE1385_TASK
        or metadata.runat != SOURCE1385_TARGET
        or metadata.managerat != SOURCE1385_MANAGER
        or metadata.is_manager
        or metadata.status not in ACTIVE_TASK_STATUSES
        or metadata.tool != "codex"
        or not metadata.pending_task_items
        or has_pending_marker(text)
    ):
        raise DrainError("Source-1385 source task is not the exact live ordinary mailbox worker")
    old_after = update_frontmatter_status(render_pending_items(text, ()), "done", "", root)
    old_after += f"\n(Source-1385 custody moved to `{successor_task}` at `{successor_target}`.)\n"
    successor = update_frontmatter_status(text, "blocked", SOURCE1385_OPERATION_BLOCKER, root)
    successor_text = source1385_replace_frontmatter(
        successor,
        {"runat": successor_target, "managerat": successor_manager},
        frozenset({"session_id"}),
    )
    successor_metadata = parse_task_metadata(successor_text, root)
    old_metadata = parse_task_metadata(old_after, root)
    if (
        successor_metadata is None
        or successor_metadata.status != "blocked"
        or successor_metadata.runat != successor_target
        or successor_metadata.managerat != successor_manager
        or successor_metadata.pending_task_items != metadata.pending_task_items
        or successor_metadata.is_manager
        or successor_metadata.tool != metadata.tool
        or successor_metadata.session_id
        or old_metadata is None
        or old_metadata.status != "done"
        or old_metadata.pending_task_items
    ):
        raise DrainError("Source-1385 successor bytes lost exact task custody")
    return old_after.encode(), successor_text.encode()


def bind_source1385_live_worker_handoff(
    root: Path,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    output: Path,
    executor: str,
    task_sha256: str,
    todo_sha256: str,
    successor_task: str,
    successor_target: str,
    successor_eligibility: Path,
    successor_eligibility_sha256: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    successor_target = canonical_target(successor_target)
    if successor_target == SOURCE1385_TARGET or successor_target.startswith("h"):
        raise DrainError("Source-1385 successor target must be distinct and non-Human")
    if not successor_task.endswith(".md") or "/" in successor_task or successor_task == SOURCE1385_TASK:
        raise DrainError("Source-1385 successor task name is invalid")
    executor_pane_id = authenticate_executor(executor)
    review = validate_source1385_code_review(root, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, executor)
    task = root / SOURCE1385_TASK
    successor = root / successor_task
    todo = root / "TODO.md"
    if successor.exists() or successor.is_symlink():
        raise DrainError("Source-1385 successor task path collides")
    if successor_eligibility.resolve().is_relative_to(root):
        raise DrainError("Source-1385 successor eligibility must be disjoint from work_logs")
    eligibility_data = owner_private_file_bytes(successor_eligibility.resolve(), "Source-1385 successor eligibility")
    eligibility = validate_source1385_successor_eligibility(
        eligibility_data,
        successor_eligibility_sha256,
        successor_target,
        successor_task,
    )
    successor_manager = str(eligibility["successor_manager"])
    manager_task = (root / str(eligibility["successor_manager_task"])).resolve()
    if not manager_task.is_relative_to(root) or manager_task in {task, todo, successor}:
        raise DrainError("Source-1385 successor manager task is unsafe")
    with root_membership_lock(root), task_target_lock(root, SOURCE1385_TARGET), task_target_lock(root, successor_target), ExitStack() as locks:
        graph_paths = lock_complete_task_records(root, locks, {task, successor, todo, manager_task})
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted during Source-1385 binding")
        authority = source1385_authority(root)
        task_data = task_bytes_no_follow(root, task)
        todo_data = task_bytes_no_follow(root, todo)
        if sha256_bytes(task_data) != task_sha256 or sha256_bytes(todo_data) != todo_sha256:
            raise DrainError("Source-1385 task or TODO digest drifted")
        metadata = parse_task_metadata(task_data.decode("utf-8"), root)
        if metadata is None:
            raise DrainError("Source-1385 task metadata is missing")
        if metadata.session_id and metadata.session_id != eligibility.get("source_session_id"):
            raise DrainError("Source-1385 task and accepted source session identities differ")
        if task_records_for_target(root, SOURCE1385_TARGET, active_only=True) != (task.resolve(),):
            raise DrainError("Source-1385 source target does not have singular active ownership")
        if task_records_for_target(root, successor_target, active_only=True):
            raise DrainError("Source-1385 successor target already has active ownership")
        todo_row = source1385_todo_row(root, todo_data, task, SOURCE1385_TARGET)
        state = inspect_target(SOURCE1385_TARGET)
        if state not in STOPPABLE:
            raise DrainError("Source-1385 source target is not a supported live worker state")
        identity = target_identity(SOURCE1385_TARGET, state)
        successor_state = inspect_target(successor_target)
        if successor_state != "absent":
            raise DrainError("Source-1385 successor target is not exactly absent")
        current_manager_task, current_manager_data = active_manager_owner(root, successor_manager)
        source_manager_task, source_manager_data = active_manager_owner(root, SOURCE1385_MANAGER)
        if (
            current_manager_task != manager_task
            or sha256_bytes(current_manager_data) != eligibility["successor_manager_task_sha256"]
            or exact_pane_id(successor_manager) == ""
        ):
            raise DrainError("Source-1385 successor manager acceptance drifted")
        source_manager_pane_id = exact_pane_id(SOURCE1385_MANAGER)
        if source_manager_pane_id == "":
            raise DrainError("Source-1385 source manager authority is not live")
        old_replacement, successor_data = source1385_task_replacements(
            root,
            task,
            task_data,
            successor_task,
            successor_target,
            successor_manager,
        )
        todo_replacement = source1385_todo_replacement(root, todo_data, task, successor, successor_target)
        graph = capture_locked_active_graph(root, graph_paths)
        dirty = repository_dirty_snapshot(root, (task, todo, successor))
        if source1385_authority(root) != authority:
            raise DrainError("Source-1386 authority drifted during Source-1385 binding")
        if owner_private_file_bytes(successor_eligibility.resolve(), "Source-1385 successor eligibility") != eligibility_data:
            raise DrainError("Source-1385 successor eligibility drifted during binding")
        value: dict[str, object] = {
            "schema": SOURCE1385_BINDING_SCHEMA,
            "kind": "source1385-live-worker-handoff",
            "source": {
                "path": SOURCE1385_TASK,
                "sha256": sha256_bytes(task_data),
                "source_base64": base64.b64encode(task_data).decode("ascii"),
                "replacement_base64": base64.b64encode(old_replacement).decode("ascii"),
                "metadata": {
                    "status": metadata.status,
                    "runat": metadata.runat,
                    "managerat": metadata.managerat,
                    "is_manager": metadata.is_manager,
                    "pending_task_items": list(metadata.pending_task_items),
                    "session_id": metadata.session_id,
                },
            },
            "successor": {
                "path": successor_task,
                "target": successor_target,
                "sha256": sha256_bytes(successor_data),
                "source_base64": base64.b64encode(successor_data).decode("ascii"),
                "eligibility": eligibility,
                "eligibility_path": str(successor_eligibility.resolve()),
                "eligibility_sha256": successor_eligibility_sha256,
            },
            "todo": {
                "path": "TODO.md",
                "sha256": sha256_bytes(todo_data),
                "source_base64": base64.b64encode(todo_data).decode("ascii"),
                "replacement_base64": base64.b64encode(todo_replacement).decode("ascii"),
                "current_row": todo_row,
            },
            "source_target": {
                "target": SOURCE1385_TARGET,
                "state": state,
                "identity": {
                    "pane_id": identity[0],
                    "pane_pid": identity[1],
                    "pane_start_ticks": identity[2],
                },
                "session_id": eligibility["source_session_id"],
            },
            "manager": {
                "source_target": SOURCE1385_MANAGER,
                "source_task": str(source_manager_task.relative_to(root)),
                "source_task_sha256": sha256_bytes(source_manager_data),
                "source_pane_id": source_manager_pane_id,
                "successor_target": successor_manager,
                "successor_task": str(manager_task.relative_to(root)),
                "successor_task_sha256": sha256_bytes(current_manager_data),
                "successor_pane_id": exact_pane_id(successor_manager),
            },
            "active_graph": graph_projection_value(graph.active_rows),
            "dirty_manifest": dirty,
            "human_authority": authority,
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "executor_pane_id": executor_pane_id,
            "email_policy": "suppressed",
            "mailbox_policy": "no-mailbox-access",
            "pane_policy": "no-production-stop-during-binding",
            "pending_policy": "preserved",
            "execution_policy": "separate-reviewed-production-packet-required",
        }
        protected = {task, todo, successor, successor_eligibility.resolve(), review_path.resolve(), consumed_receipt.resolve(), root / "manager_mail" / SOURCE1385_AUTHORITY_NAME, *root.rglob("*.md")}
        return write_binding(output.resolve(), root, value, protected)


def parse_source1385_binding(data: bytes) -> dict[str, object]:
    try:
        binding = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("Source-1385 binding is not JSON") from error
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != SOURCE1385_BINDING_SCHEMA
        or binding.get("kind") != "source1385-live-worker-handoff"
        or binding.get("email_policy") != "suppressed"
        or binding.get("mailbox_policy") != "no-mailbox-access"
        or binding.get("pending_policy") != "preserved"
        or binding.get("execution_policy") != "separate-reviewed-production-packet-required"
    ):
        raise DrainError("Source-1385 binding has the wrong exact operation identity")
    return binding


def source1385_phase_identity(binding_sha256: str, review_sha256: str, audit_output: Path) -> str:
    return sha256_bytes(canonical_json({"binding_sha256": binding_sha256, "review_sha256": review_sha256, "audit_output": str(audit_output)}))


def source1385_phase_name(audit_output: Path, transaction: str, phase: str) -> str:
    return f".{audit_output.name}.source1385-{transaction[:24]}.{phase}.json"


def source1385_phase_data(transaction: str, binding_sha256: str, review_sha256: str, phase: str) -> bytes:
    return canonical_json(
        {
            "schema": "omo-namespace-drain-source1385-phase/v1",
            "transaction": transaction,
            "binding_sha256": binding_sha256,
            "review_sha256": review_sha256,
            "phase": phase,
        }
    )


def source1385_phase(
    directory: PinnedDirectory,
    audit_output: Path,
    transaction: str,
    binding_sha256: str,
    review_sha256: str,
) -> str:
    highest = -1
    missing_seen = False
    for index, phase in enumerate(SOURCE1385_PHASES):
        name = source1385_phase_name(audit_output, transaction, phase)
        if pinned_entry_state(directory, name) is None:
            missing_seen = True
            continue
        if missing_seen:
            raise DrainError("Source-1385 recovery phases are noncontiguous")
        if pinned_private_bytes(directory, name, f"Source-1385 {phase} phase") != source1385_phase_data(
            transaction,
            binding_sha256,
            review_sha256,
            phase,
        ):
            raise DrainError(f"Source-1385 {phase} phase collides with foreign bytes")
        highest = index
    return SOURCE1385_PHASES[highest] if highest >= 0 else ""


def record_source1385_phase(
    directory: PinnedDirectory,
    audit_output: Path,
    transaction: str,
    binding_sha256: str,
    review_sha256: str,
    expected: str,
    next_phase: str,
) -> str:
    expected_index = -1 if expected == "" else SOURCE1385_PHASES.index(expected)
    if SOURCE1385_PHASES.index(next_phase) != expected_index + 1:
        raise DrainError("Source-1385 phase transition is not sequential")
    if source1385_phase(directory, audit_output, transaction, binding_sha256, review_sha256) != expected:
        raise DrainError("Source-1385 phase drifted before transition")
    name = source1385_phase_name(audit_output, transaction, next_phase)
    pinned_write_new_private(directory, name, source1385_phase_data(transaction, binding_sha256, review_sha256, next_phase))
    if source1385_phase(directory, audit_output, transaction, binding_sha256, review_sha256) != next_phase:
        raise DrainError("Source-1385 phase did not commit")
    return next_phase


def publish_source1385_successor(root: Path, path: Path, data: bytes, mode: int) -> None:
    with lifecycle_parent_descriptor(root, path) as (directory_fd, _, _):
        existing = lifecycle_entry_bytes(directory_fd, path.name, "Source-1385 successor")
        if existing is not None:
            os.close(existing[0])
            raise DrainError("Source-1385 successor path collided before publication")
        descriptor = os.open(
            path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(descriptor, mode)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise DrainError("Source-1385 successor publication made no progress")
                view = view[written:]
            os.fsync(descriptor)
            held_data, held_state = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, "Source-1385 successor")
            public = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if held_data != data or stable_stat_identity(public) != stable_stat_identity(held_state):
                raise DrainError("Source-1385 successor identity drifted during publication")
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)


def source1385_foreign_graph(binding: dict[str, object], snapshot: ActiveGraphSnapshot, source_path: str, successor_path: str) -> tuple[dict[str, object], ...]:
    projection = binding.get("active_graph")
    bound = projection.get("tasks") if isinstance(projection, dict) else None
    if not isinstance(bound, list):
        raise DrainError("Source-1385 binding lacks its active graph")
    excluded = {source_path, successor_path}
    bound_foreign = tuple(item for item in bound if isinstance(item, dict) and item.get("path") not in excluded)
    current_foreign = tuple(asdict(row) for row in snapshot.active_rows if row.path not in excluded)
    if bound_foreign != current_foreign:
        raise DrainError("Source-1385 foreign active graph drifted")
    return current_foreign


def source1385_target_absent(target: str, pane_id: str, pane_pid: int, pane_start_ticks: int) -> None:
    if recovery_pane_presence(target, pane_id) != "absent":
        raise DrainError("Source-1385 source pane remains present")
    observed_ticks = process_start_ticks(pane_pid)
    if observed_ticks == pane_start_ticks:
        raise DrainError("Source-1385 source process remains alive after pane removal")


# 🧑 "Also close all cedit agents and move out their tasks"
def close_source1385_live_worker_handoff(
    root: Path,
    binding_path: Path,
    binding_sha256: str,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    audit_output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    raise DrainError(SOURCE1385_PUBLIC_MUTATION_BLOCKER)
    audit_output = audit_output.absolute()
    binding_data = private_file_bytes(binding_path, binding_sha256, "Source-1385 binding")
    binding = parse_source1385_binding(binding_data)
    executor_pane_id = authenticate_executor(executor)
    validate_binding_route(binding, review_sha256, executor, executor_pane_id)
    source_data, source_replacement = binding_entry(binding, "source", SOURCE1385_TASK)
    todo_data, todo_replacement = binding_entry(binding, "todo", "TODO.md")
    raw_successor = binding.get("successor")
    raw_target = binding.get("source_target")
    raw_manager = binding.get("manager")
    if not isinstance(raw_successor, dict) or not isinstance(raw_target, dict) or not isinstance(raw_manager, dict):
        raise DrainError("Source-1385 binding lacks successor, target, or manager identity")
    successor_name = raw_successor.get("path")
    successor_target = raw_successor.get("target")
    successor_data_value = raw_successor.get("source_base64")
    successor_manager = raw_manager.get("successor_target")
    manager_task_name = raw_manager.get("successor_task")
    source_manager_task_name = raw_manager.get("source_task")
    identity = raw_target.get("identity")
    session_id = raw_target.get("session_id")
    if (
        not isinstance(successor_name, str)
        or not isinstance(successor_target, str)
        or not isinstance(successor_data_value, str)
        or not isinstance(successor_manager, str)
        or not isinstance(manager_task_name, str)
        or not isinstance(source_manager_task_name, str)
        or not isinstance(identity, dict)
        or not isinstance(session_id, str)
    ):
        raise DrainError("Source-1385 binding has malformed transfer identities")
    try:
        successor_data = base64.b64decode(successor_data_value, validate=True)
        pane_id = str(identity["pane_id"])
        pane_pid = int(identity["pane_pid"])
        pane_start_ticks = int(identity["pane_start_ticks"])
    except (ValueError, KeyError, TypeError) as error:
        raise DrainError("Source-1385 binding has invalid encoded identities") from error
    source = root / SOURCE1385_TASK
    todo = root / "TODO.md"
    successor = root / successor_name
    manager_task = root / manager_task_name
    source_manager_task = root / source_manager_task_name
    eligibility_path = Path(str(raw_successor.get("eligibility_path", ""))).resolve()
    eligibility_sha256 = str(raw_successor.get("eligibility_sha256", ""))
    transaction = source1385_phase_identity(binding_sha256, review_sha256, audit_output)
    validate_recovery_output_path(
        audit_output,
        root,
        {
            binding_path.resolve(),
            review_path.resolve(),
            consumed_receipt.resolve(),
            eligibility_path,
            source,
            todo,
            successor,
            manager_task,
            source_manager_task,
        },
        allow_existing=True,
    )
    with (
        root_membership_lock(root),
        task_target_lock(root, SOURCE1385_TARGET),
        task_target_lock(root, successor_target),
        pinned_directory(audit_output.parent) as output_directory,
        ExitStack() as locks,
    ):
        graph_paths = lock_complete_task_records(root, locks, {source, todo, successor, manager_task, source_manager_task})
        phase = source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256)
        pane_presence = recovery_pane_presence(SOURCE1385_TARGET, pane_id)
        if not phase or pane_presence == "present":
            _ = validate_source1385_code_review(
                root,
                review_path,
                review_sha256,
                consumed_receipt,
                consumed_receipt_sha256,
                executor,
            )
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted during Source-1385 reconciliation")
        if source1385_authority(root) != binding.get("human_authority"):
            raise DrainError("Source-1386 authority drifted during Source-1385 reconciliation")
        eligibility_data = owner_private_file_bytes(eligibility_path, "Source-1385 successor eligibility")
        eligibility = validate_source1385_successor_eligibility(
            eligibility_data,
            eligibility_sha256,
            successor_target,
            successor_name,
        )
        if eligibility != raw_successor.get("eligibility") or eligibility.get("source_session_id") != session_id:
            raise DrainError("Source-1385 successor acceptance drifted")
        manager_path, manager_data = active_manager_owner(root, successor_manager)
        if (
            manager_path != manager_task.resolve()
            or sha256_bytes(manager_data) != raw_manager.get("successor_task_sha256")
            or exact_pane_id(successor_manager) != raw_manager.get("successor_pane_id")
        ):
            raise DrainError("Source-1385 successor manager drifted")
        source_manager_path, source_manager_data = active_manager_owner(root, SOURCE1385_MANAGER)
        if (
            source_manager_path != source_manager_task.resolve()
            or sha256_bytes(source_manager_data) != raw_manager.get("source_task_sha256")
            or exact_pane_id(SOURCE1385_MANAGER) != raw_manager.get("source_pane_id")
        ):
            raise DrainError("Source-1385 source manager authority drifted")
        derived_source, derived_successor = source1385_task_replacements(
            root,
            source,
            source_data,
            successor_name,
            successor_target,
            successor_manager,
        )
        derived_todo = source1385_todo_replacement(root, todo_data, source, successor, successor_target)
        if (derived_source, derived_todo, derived_successor) != (source_replacement, todo_replacement, successor_data):
            raise DrainError("Source-1385 binding does not contain canonical lifecycle replacements")
        source_current = task_bytes_no_follow(root, source)
        todo_current = task_bytes_no_follow(root, todo)
        successor_current = None if not successor.exists() and not successor.is_symlink() else task_bytes_no_follow(root, successor)
        source_state = exact_file_transition_state(source_current, source_data, source_replacement, "Source-1385 source")
        todo_state = exact_file_transition_state(todo_current, todo_data, todo_replacement, "Source-1385 TODO")
        if successor_current not in {None, successor_data}:
            raise DrainError("Source-1385 successor path contains foreign bytes")
        _ = source1385_foreign_graph(binding, capture_locked_active_graph(root, graph_paths), SOURCE1385_TASK, successor_name)
        validate_dirty_snapshot(root, (source, todo, successor), binding.get("dirty_manifest"))
        if inspect_target(successor_target) != "absent" or task_records_for_target(root, successor_target, active_only=True) not in {(), (successor.resolve(),)}:
            raise DrainError("Source-1385 successor target or ownership collided")
        if not phase:
            if source_state != "source" or todo_state != "source" or successor_current is not None or pane_presence != "present":
                raise DrainError("fresh Source-1385 transaction does not match its exact initial state")
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, "", "prepared")
        if phase == "prepared":
            if source_state != "source" or todo_state != "source" or successor_current is not None or pane_presence != "present":
                raise DrainError("prepared Source-1385 transaction has inconsistent state")
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, phase, "kill-started")
        if phase == "kill-started":
            if pane_presence == "present":
                if target_identity(SOURCE1385_TARGET, inspect_target(SOURCE1385_TARGET)) != (pane_id, pane_pid, pane_start_ticks):
                    raise DrainError("Source-1385 source pane identity drifted before stop")
                stop_target(SOURCE1385_TARGET, pane_id, pane_pid, pane_start_ticks, session_id)
            source1385_target_absent(SOURCE1385_TARGET, pane_id, pane_pid, pane_start_ticks)
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, phase, "pane-removed")
        else:
            source1385_target_absent(SOURCE1385_TARGET, pane_id, pane_pid, pane_start_ticks)
        if phase == "pane-removed":
            if source_state == "source":
                atomic_replace_task(root, source, source_data, source_replacement)
                source_state = "replacement"
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, phase, "old-closed")
        if source_state != "replacement":
            raise DrainError("Source-1385 old task is not durably closed")
        if phase == "old-closed":
            if todo_state == "source":
                atomic_replace_task(root, todo, todo_data, todo_replacement)
                todo_state = "replacement"
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, phase, "todo-moved")
        if todo_state != "replacement":
            raise DrainError("Source-1385 TODO did not reach its exact replacement")
        if phase == "todo-moved":
            if successor_current is None:
                publish_source1385_successor(root, successor, successor_data, 0o644)
                successor_current = successor_data
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, phase, "successor-published")
        if successor_current != successor_data:
            raise DrainError("Source-1385 successor is not durably published")
        if task_records_for_target(root, SOURCE1385_TARGET, active_only=True) or task_records_for_target(root, successor_target, active_only=True) != (successor.resolve(),):
            raise DrainError("Source-1385 final ownership is not singular")
        if phase == "successor-published":
            audit: dict[str, object] = {
                "schema": SOURCE1385_AUDIT_SCHEMA,
                "transaction": transaction,
                "binding_sha256": binding_sha256,
                "code_review_sha256": review_sha256,
                "source_task": SOURCE1385_TASK,
                "source_target": SOURCE1385_TARGET,
                "source_pane_id": pane_id,
                "source_task_before_sha256": sha256_bytes(source_data),
                "source_task_after_sha256": sha256_bytes(source_replacement),
                "successor_task": successor_name,
                "successor_target": successor_target,
                "successor_task_sha256": sha256_bytes(successor_data),
                "todo_before_sha256": sha256_bytes(todo_data),
                "todo_after_sha256": sha256_bytes(todo_replacement),
                "email_policy": "suppressed",
                "mailbox_policy": "untouched",
                "pending_policy": "moved-once",
            }
            audit_data = canonical_json(audit)
            if pinned_entry_state(output_directory, audit_output.name) is None:
                pinned_write_new_private(output_directory, audit_output.name, audit_data)
            elif pinned_private_bytes(output_directory, audit_output.name, "Source-1385 audit") != audit_data:
                raise DrainError("Source-1385 audit output collides with foreign bytes")
            phase = record_source1385_phase(output_directory, audit_output, transaction, binding_sha256, review_sha256, phase, "complete")
        if phase != "complete":
            raise DrainError("Source-1385 transaction did not reach complete")
        audit_data = pinned_private_bytes(output_directory, audit_output.name, "Source-1385 audit")
        try:
            result = json.loads(audit_data)
        except json.JSONDecodeError as error:
            raise DrainError("Source-1385 audit is not JSON") from error
        if not isinstance(result, dict) or result.get("transaction") != transaction:
            raise DrainError("Source-1385 audit has the wrong transaction identity")
        return result


def parse_shell_binding(data: bytes, kind: str, target: str) -> tuple[dict[str, object], ShellSnapshot]:
    try:
        binding = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("shell binding is not valid JSON") from error
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != SHELL_BINDING_SCHEMA
        or binding.get("kind") != kind
        or binding.get("target") != target
        or not isinstance(binding.get("human_authority"), dict)
        or binding.get("email_policy") != "suppressed"
    ):
        raise DrainError("shell binding has the wrong exact operation identity")
    return binding, parse_shell_snapshot(binding.get("shell"))


def load_binding(path: Path, expected_sha256: str, kind: str, target: str) -> tuple[dict[str, object], ShellSnapshot]:
    return parse_shell_binding(private_file_bytes(path, expected_sha256, "shell binding"), kind, target)


def load_unslop_binding(path: Path, expected_sha256: str) -> tuple[dict[str, object], SharedPaneSnapshot]:
    data = private_file_bytes(path, expected_sha256, "Unslop binding")
    try:
        binding = json.loads(data)
        raw_pane = binding.get("shared_pane") if isinstance(binding, dict) else None
        if not isinstance(raw_pane, dict):
            raise KeyError("shared_pane")
        pane = SharedPaneSnapshot(
            str(raw_pane["pane_id"]),
            int(raw_pane["pane_pid"]),
            int(raw_pane["pane_start_ticks"]),
            str(raw_pane["pane_current_command"]),
            str(raw_pane["agent_status"]),
            int(raw_pane["launcher_pid"]),
            int(raw_pane["launcher_start_ticks"]),
            str(raw_pane["launcher_command"]),
            str(raw_pane["launcher_cmdline_sha256"]),
            str(raw_pane["launcher_executable"]),
            int(raw_pane["launcher_executable_dev"]),
            int(raw_pane["launcher_executable_ino"]),
            int(raw_pane["agent_pid"]),
            int(raw_pane["agent_start_ticks"]),
            str(raw_pane["agent_command"]),
            str(raw_pane["agent_cmdline_sha256"]),
            str(raw_pane["agent_executable"]),
            int(raw_pane["agent_executable_dev"]),
            int(raw_pane["agent_executable_ino"]),
            str(raw_pane["agent_lineage_sha256"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise DrainError("Unslop binding is invalid") from error
    if (
        binding.get("schema") != UNSLOP_BINDING_SCHEMA
        or binding.get("kind") != "unslop-shared-pane-done"
        or binding.get("accepted_completion_evidence") != UNSLOP_ACCEPTED_EVIDENCE
        or not isinstance(binding.get("human_authority"), dict)
        or not isinstance(binding.get("graph_cas"), dict)
        or binding.get("email_policy") != "suppressed"
        or binding.get("pending_policy") != "unchanged"
        or binding.get("todo_policy") != "unchanged"
        or binding.get("pane_policy") != "identity-only"
        or pane.pane_id != UNSLOP_PANE
        or pane.pane_pid <= 0
        or pane.pane_start_ticks <= 0
        or pane.pane_current_command not in {"bunx", "npx", "codex", "agent"}
        or pane.agent_status not in {"ready", "running"}
        or pane.launcher_pid <= 0
        or pane.launcher_start_ticks <= 0
        or pane.launcher_command != pane.pane_current_command
        or SHA256_RE.fullmatch(pane.launcher_cmdline_sha256) is None
        or not pane.launcher_executable.startswith("/")
        or pane.launcher_executable_dev <= 0
        or pane.launcher_executable_ino <= 0
        or pane.agent_pid <= 0
        or pane.agent_start_ticks <= 0
        or pane.agent_command not in {"codex", "agent"}
        or SHA256_RE.fullmatch(pane.agent_cmdline_sha256) is None
        or not pane.agent_executable.startswith("/")
        or pane.agent_executable_dev <= 0
        or pane.agent_executable_ino <= 0
        or SHA256_RE.fullmatch(pane.agent_lineage_sha256) is None
    ):
        raise DrainError("Unslop binding has the wrong exact operation identity")
    return binding, pane


def binding_unslop_graph_cas(binding: dict[str, object], root: Path) -> tuple[Path, str, dict[str, object]]:
    raw = binding.get("graph_cas")
    if not isinstance(raw, dict) or set(raw) != {
        "path",
        "sha256",
        "lock_generation_sha256",
        "pre_projection_sha256",
        "post_projection_sha256",
    }:
        raise DrainError("Unslop binding lacks one exact graph/CAS identity")
    raw_path = raw.get("path")
    raw_sha256 = raw.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(raw_sha256, str) or SHA256_RE.fullmatch(raw_sha256) is None:
        raise DrainError("Unslop binding graph/CAS path or digest is invalid")
    path = Path(raw_path)
    if not path.is_absolute():
        raise DrainError("Unslop binding graph/CAS path is not absolute")
    path = path.resolve()
    if path.is_relative_to(root) or path.is_relative_to(UNSLOP_RESULT_ROOT.resolve()):
        raise DrainError("Unslop graph/CAS overlaps a protected repository")
    value = load_unslop_graph_cas(path, raw_sha256)
    generation = value.get("record_generation")
    pre = value.get("pre")
    post = value.get("post")
    if (
        not isinstance(generation, dict)
        or not isinstance(pre, dict)
        or not isinstance(post, dict)
        or raw.get("lock_generation_sha256") != generation.get("paths_sha256")
        or raw.get("pre_projection_sha256") != pre.get("sha256")
        or raw.get("post_projection_sha256") != post.get("sha256")
    ):
        raise DrainError("Unslop binding graph/CAS projections do not match the immutable input")
    return path, raw_sha256, value


def reconciliation_preflight(
    root: Path,
    binding_path: Path,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    audit_output: Path,
    executor: str,
    allow_existing_output: bool = False,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    authenticate_executor(executor)
    review = validate_code_review(root, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, executor)
    if not allow_existing_output:
        validate_new_private_output(
            audit_output,
            root,
            {binding_path.resolve(), review_path.resolve(), consumed_receipt.resolve(), *root.rglob("*.md")},
        )
    return review


def validate_binding_route(binding: dict[str, object], review_sha256: str, executor: str, executor_pane_id: str) -> None:
    if binding.get("code_review_sha256") != review_sha256 or binding.get("executor") != executor or binding.get("executor_pane_id") != executor_pane_id:
        raise DrainError("shell binding is not bound to this reviewed executor route")


def bound_namespace_authority(binding: dict[str, object]) -> dict[str, str]:
    expected = {
        "path": str(TRUSTED_ROOT / "manager_mail" / AUTHORITY_NAME),
        "locator": AUTHORITY_LOCATOR,
        "sha256": AUTHORITY_SHA256,
    }
    if binding.get("human_authority") != expected:
        raise DrainError("shell binding is not bound to the exact authenticated Source-1261 directive")
    return expected


def stage_private_output(output: Path, data: bytes) -> Path:
    staged = output.parent / f".{output.name}.namespace-drain-staged-{os.getpid()}-{os.urandom(8).hex()}"
    write_new_private_output(staged, data)
    return staged


def publish_staged_output(staged: Path, output: Path) -> None:
    try:
        os.link(staged, output, follow_symlinks=False)
        directory_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        staged.unlink()
    except OSError as error:
        raise DrainError(f"irreversible operation completed; publish the retained staged audit at {staged}") from error


def pinned_entry_state(directory: PinnedDirectory, name: str) -> os.stat_result | None:
    directory.validate()
    try:
        return os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def pinned_private_bytes(directory: PinnedDirectory, name: str, label: str) -> bytes:
    directory.validate()
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.descriptor)
    except OSError as error:
        raise DrainError(f"cannot open {label} through its pinned directory: {error}") from error
    try:
        data, details = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, label)
    finally:
        os.close(descriptor)
    public = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    if (
        details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or stable_stat_identity(public) != stable_stat_identity(details)
    ):
        raise DrainError(f"{label} has an unsafe or drifting pinned identity")
    directory.validate()
    return data


def pinned_held_entry(
    directory: PinnedDirectory,
    name: str,
    label: str,
) -> tuple[int, bytes, os.stat_result] | None:
    directory.validate()
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory.descriptor,
        )
    except FileNotFoundError:
        return None
    try:
        data, details = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, label)
        public = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        if (
            details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or stable_stat_identity(public) != stable_stat_identity(details)
        ):
            raise DrainError(f"{label} has an unsafe or drifting pinned identity")
        return descriptor, data, details
    except Exception:
        os.close(descriptor)
        raise


def pinned_held_object(
    directory: PinnedDirectory,
    name: str,
    expected_mode: int,
    label: str,
) -> tuple[int, bytes, os.stat_result] | None:
    """Open one exact regular object without imposing a public-file mode."""
    directory.validate()
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory.descriptor,
        )
    except FileNotFoundError:
        return None
    try:
        data, details = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, label)
        public = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        if (
            details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != expected_mode
            or stable_stat_identity(public) != stable_stat_identity(details)
        ):
            raise DrainError(f"{label} has an unsafe or drifting pinned identity")
        return descriptor, data, details
    except Exception:
        os.close(descriptor)
        raise


def pinned_write_new_object(directory: PinnedDirectory, name: str, data: bytes, mode: int) -> None:
    if not name or "/" in name or mode & ~0o777:
        raise DrainError("new pinned object has an invalid name or mode")
    directory.validate()
    flags = os.O_RDWR | os.O_TMPFILE | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(".", flags, mode, dir_fd=directory.descriptor)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise DrainError("pinned private output write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
        linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        linkat.restype = ctypes.c_int
        if linkat(descriptor, b"", directory.descriptor, name.encode(), AT_EMPTY_PATH) != 0:
            error_number = ctypes.get_errno()
            if error_number != errno.ENOENT:
                raise OSError(error_number, os.strerror(error_number), name)
            if linkat(AT_FDCWD, f"/proc/self/fd/{descriptor}".encode(), directory.descriptor, name.encode(), AT_SYMLINK_FOLLOW) != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number), name)
        public = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        held = os.fstat(descriptor)
        if (
            exchange_object_identity(held) != exchange_object_identity(final)
            or exchange_object_identity(public) != exchange_object_identity(final)
        ):
            raise DrainError("new pinned private output did not publish its exact held object")
        os.fsync(directory.descriptor)
        directory.validate()
    finally:
        os.close(descriptor)


def pinned_write_new_private(directory: PinnedDirectory, name: str, data: bytes) -> None:
    pinned_write_new_object(directory, name, data, 0o600)


def pinned_object_binding(name: str, data: bytes, details: os.stat_result) -> dict[str, object]:
    return {
        "name": name,
        "sha256": sha256_bytes(data),
        "object": exchange_object_value(details),
    }


def bind_pinned_audit_object(directory: PinnedDirectory, name: str, expected_data: bytes) -> dict[str, object]:
    entry = pinned_held_entry(directory, name, "staged shell audit")
    if entry is None:
        raise DrainError("staged shell audit is absent")
    descriptor, data, details = entry
    try:
        if data != expected_data:
            raise DrainError("staged shell audit drifted")
        return pinned_object_binding(name, expected_data, details)
    finally:
        os.close(descriptor)


def validate_held_staged_audit(
    directory: PinnedDirectory,
    descriptor: int,
    expected_state: os.stat_result,
    binding: object,
    staged_name: str,
    expected_data: bytes,
    expected_links: int,
) -> None:
    data, held = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, "held staged shell audit")
    public = os.stat(staged_name, dir_fd=directory.descriptor, follow_symlinks=False)
    if (
        data != expected_data
        or exchange_object_identity(held) != exchange_object_identity(expected_state)
        or (expected_links == 1 and stable_stat_identity(held) != stable_stat_identity(expected_state))
        or exchange_object_identity(public) != exchange_object_identity(expected_state)
        or stable_stat_identity(public) != stable_stat_identity(held)
        or held.st_nlink != expected_links
        or not isinstance(binding, dict)
        or not exchange_object_matches(held, binding.get("object"))
    ):
        raise DrainError("held staged shell audit identity drifted")


def validate_held_shell_recovery_journal(
    directory: PinnedDirectory,
    descriptor: int,
    expected_state: os.stat_result,
    journal_name: str,
    identity: dict[str, object],
) -> None:
    expected_data = recovery_journal_bytes(identity, "prepared")
    data, held = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, "held shell recovery journal")
    try:
        public = os.stat(journal_name, dir_fd=directory.descriptor, follow_symlinks=False)
    except OSError as error:
        raise DrainError("shell recovery journal identity drifted before pane operation") from error
    if (
        data != expected_data
        or stable_stat_identity(held) != stable_stat_identity(expected_state)
        or stable_stat_identity(public) != stable_stat_identity(held)
        or held.st_nlink != 1
    ):
        raise DrainError("shell recovery journal identity drifted before pane operation")


def validate_pinned_object_binding(
    directory: PinnedDirectory,
    binding: object,
    expected_data: bytes,
    expected_mode: int,
    label: str,
) -> tuple[int, os.stat_result]:
    if not isinstance(binding, dict) or set(binding) != {"name", "sha256", "object"}:
        raise DrainError(f"{label} binding has the wrong schema")
    name = binding.get("name")
    if not isinstance(name, str) or not name or "/" in name or binding.get("sha256") != sha256_bytes(expected_data):
        raise DrainError(f"{label} binding has an invalid name or digest")
    entry = pinned_held_object(directory, name, expected_mode, label)
    if entry is None:
        raise DrainError(f"{label} is absent")
    descriptor, data, details = entry
    if data != expected_data or not exchange_object_matches(details, binding.get("object")):
        os.close(descriptor)
        raise DrainError(f"{label} identity or bytes drifted")
    return descriptor, details


def validate_pinned_bound_object(
    directory: PinnedDirectory,
    name: str,
    expected_data: bytes,
    expected_state: os.stat_result,
    expected_mode: int,
    label: str,
) -> tuple[int, os.stat_result]:
    entry = pinned_held_object(directory, name, expected_mode, label)
    if entry is None:
        raise DrainError(f"{label} is absent")
    descriptor, data, details = entry
    if data != expected_data or exchange_object_identity(details) != exchange_object_identity(expected_state):
        os.close(descriptor)
        raise DrainError(f"{label} identity or bytes drifted")
    return descriptor, details


def revalidate_lifecycle_exchanged_entry(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected_data: bytes,
    expected_state: os.stat_result,
    label: str,
) -> None:
    data, held = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, label)
    public = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        data != expected_data
        or exchange_object_identity(held) != exchange_object_identity(expected_state)
        or exchange_object_identity(public) != exchange_object_identity(expected_state)
    ):
        raise DrainError(f"{label} identity or bytes drifted")


def pinned_publish(directory: PinnedDirectory, staged_name: str, output_name: str, expected: bytes) -> None:
    directory.validate()
    stage_fd = os.open(staged_name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.descriptor)
    try:
        stage_data, stage_state = read_bounded_fd(stage_fd, MAX_PRIVATE_ARTIFACT_BYTES, "staged shell audit")
        public_stage = os.stat(staged_name, dir_fd=directory.descriptor, follow_symlinks=False)
        if stage_data != expected or stable_stat_identity(public_stage) != stable_stat_identity(stage_state):
            raise DrainError("staged shell audit drifted before publication")
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
        linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        linkat.restype = ctypes.c_int
        if linkat(stage_fd, b"", directory.descriptor, output_name.encode(), AT_EMPTY_PATH) != 0:
            error_number = ctypes.get_errno()
            if error_number != errno.ENOENT:
                raise OSError(error_number, os.strerror(error_number), output_name)
            if linkat(AT_FDCWD, f"/proc/self/fd/{stage_fd}".encode(), directory.descriptor, output_name.encode(), AT_SYMLINK_FOLLOW) != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number), output_name)
        output_fd = os.open(output_name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.descriptor)
        try:
            output_data, output_state = read_bounded_fd(output_fd, MAX_PRIVATE_ARTIFACT_BYTES, "published shell audit")
        finally:
            os.close(output_fd)
        held_after = os.fstat(stage_fd)
        public_stage = os.stat(staged_name, dir_fd=directory.descriptor, follow_symlinks=False)
        public_output = os.stat(output_name, dir_fd=directory.descriptor, follow_symlinks=False)
        if (
            output_data != expected
            or file_content_identity(held_after) != file_content_identity(stage_state)
            or stable_stat_identity(output_state) != stable_stat_identity(held_after)
            or stable_stat_identity(public_stage) != stable_stat_identity(held_after)
            or stable_stat_identity(public_output) != stable_stat_identity(held_after)
        ):
            raise DrainError("published shell audit is not the exact held staged object")
        os.fsync(directory.descriptor)
        directory.validate()
    except OSError as error:
        raise DrainError(f"irreversible operation completed; retained staged audit {staged_name} in {directory.path}") from error
    finally:
        os.close(stage_fd)


def validate_recovery_output_path(path: Path, root: Path, protected: set[Path], allow_existing: bool) -> Path:
    if not path.is_absolute() or not path.name or ".." in path.parts:
        raise DrainError("recovery output must be one absolute lexical path")
    if path == root or path.is_relative_to(root) or path in protected:
        raise DrainError("recovery output overlaps protected state")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directory_fds: list[int] = []
    try:
        directory_fds.append(os.open("/", directory_flags))
        for part in path.parts[1:-1]:
            directory_fds.append(os.open(part, directory_flags, dir_fd=directory_fds[-1]))
        parent = os.fstat(directory_fds[-1])
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
            raise DrainError("recovery output parent must remain owner-private")
        try:
            entry = os.stat(path.name, dir_fd=directory_fds[-1], follow_symlinks=False)
        except FileNotFoundError:
            entry = None
        if entry is not None and (
            not allow_existing
            or not stat.S_ISREG(entry.st_mode)
            or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) != 0o600
        ):
            raise DrainError("recovery output collides with an unsafe or unexpected entry")
        return path
    except OSError as error:
        raise DrainError(f"cannot validate recovery output path: {error}") from error
    finally:
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def recovery_paths(audit_output: Path, binding_sha256: str, operation: str) -> tuple[Path, Path]:
    token = sha256_bytes(f"{operation}\0{binding_sha256}\0{audit_output}".encode())[:24]
    return (
        audit_output.parent / f".{audit_output.name}.namespace-drain-{token}.staged",
        audit_output.parent / f".{audit_output.name}.namespace-drain-{token}.journal",
    )


def owner_private_file_bytes(path: Path, label: str) -> bytes:
    data, details = nofollow_absolute_file_bytes(path, label)
    if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
        raise DrainError(f"{label} is not an owner-private file")
    return data


def path_entry_state(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def recovery_identity(
    operation: str,
    target: str,
    binding_path: Path,
    binding_sha256: str,
    binding_data: bytes,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    executor: str,
    executor_pane_id: str,
    authority: dict[str, str],
    expected_shell: ShellSnapshot,
    audit_output: Path,
    staged_audit: Path,
    staged_audit_binding: dict[str, object],
    output_directory: PinnedDirectory,
    audit_data: bytes,
    task: dict[str, object] | None = None,
    todo: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema": SHELL_RECOVERY_SCHEMA,
        "operation": operation,
        "target": target,
        "binding": {
            "path": str(binding_path),
            "sha256": binding_sha256,
            "source_base64": base64.b64encode(binding_data).decode("ascii"),
        },
        "review": {"path": str(review_path), "sha256": review_sha256},
        "consumed_review": {"path": str(consumed_receipt), "sha256": consumed_receipt_sha256},
        "executor": {"target": executor, "pane_id": executor_pane_id},
        "human_authority": authority,
        "shell": {
            "pane_id": expected_shell.pane_id,
            "root_pid": expected_shell.root.pid if expected_shell.root is not None else 0,
            "root_start_ticks": expected_shell.root.start_ticks if expected_shell.root is not None else 0,
            "foreground_pid": expected_shell.foreground.pid if expected_shell.foreground is not None else 0,
            "foreground_start_ticks": expected_shell.foreground.start_ticks if expected_shell.foreground is not None else 0,
        },
        "audit": {
            "path": str(audit_output),
            "sha256": sha256_bytes(audit_data),
            "staged_path": str(staged_audit),
            "staged_object": staged_audit_binding,
        },
        "output_directory": {
            "path": str(output_directory.path),
            "device": output_directory.states[-1].st_dev,
            "inode": output_directory.states[-1].st_ino,
            "uid": output_directory.states[-1].st_uid,
            "mode": stat.S_IMODE(output_directory.states[-1].st_mode),
        },
        "task": task,
        "todo": todo,
    }


def recovery_journal_bytes(identity: dict[str, object], phase: str) -> bytes:
    return canonical_json({**identity, "identity_sha256": sha256_bytes(canonical_json(identity)), "phase": phase})


def recovery_phases(operation: str) -> tuple[str, ...]:
    if operation == "taskless-empty-shell":
        return ("prepared", "kill-started", "pane-removed", "complete")
    if operation == "completed-task-shell":
        return ("prepared", "kill-started", "pane-removed", "files-exchanged", "complete")
    raise DrainError("shell recovery operation has no deterministic phase graph")


def recovery_phase_entry_name(journal_name: str, phase: str) -> str:
    return journal_name if phase == "prepared" else f"{journal_name}.{phase}"


def recovery_phase_staged_name(journal_name: str, phase: str) -> str:
    if phase == "prepared":
        raise DrainError("prepared is the commit record, not a staged future phase")
    return f"{journal_name}.staged-{phase}"


def recovery_phase_bytes(transaction_sha256: str, phase: str) -> bytes:
    if SHA256_RE.fullmatch(transaction_sha256) is None or phase == "prepared":
        raise DrainError("future recovery phase has an invalid transaction identity")
    return canonical_json(
        {
            "schema": "omo-namespace-drain-shell-phase/v1",
            "transaction_sha256": transaction_sha256,
            "phase": phase,
        }
    )


def recovery_core_identity(identity: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in identity.items() if key not in {"transaction_sha256", "phase_records"}}


def bind_recovery_phase_objects(directory: PinnedDirectory, journal_name: str, operation: str, identity: dict[str, object]) -> None:
    identity["transaction_sha256"] = "0" * 64
    identity["phase_records"] = {}
    while True:
        transaction_sha256 = sha256_bytes(canonical_json(recovery_core_identity(identity)))
        identity["transaction_sha256"] = transaction_sha256
        identity["phase_records"] = prepare_recovery_phase_objects(directory, journal_name, operation, transaction_sha256)
        stable_sha256 = sha256_bytes(canonical_json(recovery_core_identity(identity)))
        if stable_sha256 == transaction_sha256:
            return


def bind_recovery_phase_objects_held(directory: PinnedDirectory, journal_name: str, operation: str, identity: dict[str, object]) -> dict[str, HeldRecoveryPhaseObject]:
    identity["transaction_sha256"] = "0" * 64
    identity["phase_records"] = {}
    held_objects: dict[str, HeldRecoveryPhaseObject] = {}
    try:
        while True:
            for phase_object in held_objects.values():
                phase_object.close()
            held_objects = {}
            transaction_sha256 = sha256_bytes(canonical_json(recovery_core_identity(identity)))
            identity["transaction_sha256"] = transaction_sha256
            identity["phase_records"], held_objects = prepare_recovery_phase_objects_held(directory, journal_name, operation, transaction_sha256)
            stable_sha256 = sha256_bytes(canonical_json(recovery_core_identity(identity)))
            if stable_sha256 == transaction_sha256:
                return held_objects
    except Exception:
        for phase_object in held_objects.values():
            phase_object.close()
        raise


def prepare_recovery_phase_objects(
    directory: PinnedDirectory,
    journal_name: str,
    operation: str,
    transaction_sha256: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for phase in recovery_phases(operation)[1:]:
        staged_name = recovery_phase_staged_name(journal_name, phase)
        committed_name = recovery_phase_entry_name(journal_name, phase)
        data = recovery_phase_bytes(transaction_sha256, phase)
        if pinned_entry_state(directory, committed_name) is not None:
            raise DrainError("future recovery phase exists before the prepared commit point")
        if pinned_entry_state(directory, staged_name) is None:
            pinned_write_new_private(directory, staged_name, data)
        entry = pinned_held_object(directory, staged_name, 0o600, f"staged shell recovery {phase} phase")
        if entry is None:
            raise DrainError(f"staged shell recovery {phase} phase disappeared")
        descriptor, observed, details = entry
        try:
            if observed != data:
                raise DrainError(f"staged shell recovery {phase} phase has foreign bytes")
            result[phase] = {
                "staged_name": staged_name,
                "committed_name": committed_name,
                "sha256": sha256_bytes(data),
                "object": exchange_object_value(details),
            }
        finally:
            os.close(descriptor)
    return result


def prepare_recovery_phase_objects_held(
    directory: PinnedDirectory,
    journal_name: str,
    operation: str,
    transaction_sha256: str,
) -> tuple[dict[str, object], dict[str, HeldRecoveryPhaseObject]]:
    result: dict[str, object] = {}
    held_objects: dict[str, HeldRecoveryPhaseObject] = {}
    try:
        for phase in recovery_phases(operation)[1:]:
            staged_name = recovery_phase_staged_name(journal_name, phase)
            committed_name = recovery_phase_entry_name(journal_name, phase)
            data = recovery_phase_bytes(transaction_sha256, phase)
            if pinned_entry_state(directory, committed_name) is not None:
                raise DrainError("future recovery phase exists before the prepared commit point")
            if pinned_entry_state(directory, staged_name) is None:
                pinned_write_new_private(directory, staged_name, data)
            entry = pinned_held_object(directory, staged_name, 0o600, f"staged shell recovery {phase} phase")
            if entry is None:
                raise DrainError(f"staged shell recovery {phase} phase disappeared")
            descriptor, observed, details = entry
            if observed != data:
                os.close(descriptor)
                raise DrainError(f"staged shell recovery {phase} phase has foreign bytes")
            result[phase] = {
                "staged_name": staged_name,
                "committed_name": committed_name,
                "sha256": sha256_bytes(data),
                "object": exchange_object_value(details),
            }
            held_objects[phase] = HeldRecoveryPhaseObject(descriptor, details)
        return result, held_objects
    except Exception:
        for phase_object in held_objects.values():
            phase_object.close()
        raise


def validate_recovery_phase_record(
    directory: PinnedDirectory,
    transaction_sha256: str,
    phase: str,
    raw: object,
    committed: bool,
) -> None:
    if not isinstance(raw, dict) or set(raw) != {"staged_name", "committed_name", "sha256", "object"}:
        raise DrainError(f"shell recovery {phase} phase binding has the wrong schema")
    expected_staged = str(raw.get("staged_name", ""))
    expected_committed = str(raw.get("committed_name", ""))
    expected_data = recovery_phase_bytes(transaction_sha256, phase)
    if (
        expected_staged == expected_committed
        or "/" in expected_staged
        or "/" in expected_committed
        or raw.get("sha256") != sha256_bytes(expected_data)
    ):
        raise DrainError(f"shell recovery {phase} phase binding is invalid")
    name = expected_committed if committed else expected_staged
    entry = pinned_held_object(directory, name, 0o600, f"shell recovery {phase} phase")
    if entry is None:
        raise DrainError(f"shell recovery {phase} phase object is absent")
    descriptor, observed, details = entry
    try:
        if observed != expected_data or not exchange_object_matches(details, raw.get("object")):
            raise DrainError(f"shell recovery {phase} phase object drifted")
    finally:
        os.close(descriptor)


def held_recovery_phase_record(
    directory: PinnedDirectory,
    transaction_sha256: str,
    phase: str,
    raw: object,
    name_key: str,
    label: str,
) -> tuple[int, bytes, os.stat_result]:
    if not isinstance(raw, dict) or set(raw) != {"staged_name", "committed_name", "sha256", "object"}:
        raise DrainError(f"{label} binding has the wrong schema")
    name = raw.get(name_key)
    expected_data = recovery_phase_bytes(transaction_sha256, phase)
    if not isinstance(name, str) or not name or "/" in name or raw.get("sha256") != sha256_bytes(expected_data):
        raise DrainError(f"{label} binding is invalid")
    entry = pinned_held_object(directory, name, 0o600, label)
    if entry is None:
        raise DrainError(f"{label} is absent")
    descriptor, observed, details = entry
    if observed != expected_data or not exchange_object_matches(details, raw.get("object")):
        os.close(descriptor)
        raise DrainError(f"{label} object drifted")
    return descriptor, observed, details


def recovery_phase_from_entries(
    directory: PinnedDirectory,
    journal_name: str,
    identity: dict[str, object],
) -> str:
    operation = identity.get("operation")
    if not isinstance(operation, str):
        raise DrainError("shell recovery identity lacks its phase-graph operation")
    phases = recovery_phases(operation)
    transaction_sha256 = identity.get("transaction_sha256")
    records = identity.get("phase_records")
    if (
        not isinstance(transaction_sha256, str)
        or transaction_sha256 != sha256_bytes(canonical_json(recovery_core_identity(identity)))
        or not isinstance(records, dict)
        or set(records) != set(phases[1:])
    ):
        raise DrainError("shell recovery phase-object identity is invalid")
    highest = "prepared"
    suffix_started = False
    for phase in phases[1:]:
        raw = records[phase]
        if not isinstance(raw, dict):
            raise DrainError(f"shell recovery {phase} phase binding is invalid")
        committed_name = str(raw.get("committed_name", ""))
        staged_entry = pinned_entry_state(directory, str(raw.get("staged_name", ""))) is not None
        committed = pinned_entry_state(directory, committed_name) is not None
        if not staged_entry:
            raise DrainError(f"shell recovery {phase} staged evidence is absent")
        staged_fd, _, staged_details = held_recovery_phase_record(
            directory,
            transaction_sha256,
            phase,
            raw,
            "staged_name",
            f"staged shell recovery {phase} phase",
        )
        try:
            if committed:
                if suffix_started:
                    raise DrainError("shell recovery contains a noncontiguous future phase")
                committed_fd, _, committed_details = held_recovery_phase_record(
                    directory,
                    transaction_sha256,
                    phase,
                    raw,
                    "committed_name",
                    f"committed shell recovery {phase} phase",
                )
                try:
                    if not exchange_object_matches(committed_details, raw.get("object")):
                        raise DrainError(f"shell recovery {phase} committed phase is not the exact staged inode")
                finally:
                    os.close(committed_fd)
                highest = phase
            else:
                suffix_started = True
        finally:
            os.close(staged_fd)
    return highest


def probe_shell_recovery(
    output_directory: PinnedDirectory,
    audit_output: Path,
    binding_path: Path,
    binding_sha256: str,
    operation: str,
    target: str,
) -> tuple[str, bytes, dict[str, object]] | None:
    _, journal_path = recovery_paths(audit_output, binding_sha256, operation)
    if pinned_entry_state(output_directory, journal_path.name) is None:
        return None
    data = pinned_private_bytes(output_directory, journal_path.name, "shell recovery journal")
    try:
        value = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("shell recovery journal is not valid JSON") from error
    raw_binding = value.get("binding") if isinstance(value, dict) else None
    raw_audit = value.get("audit") if isinstance(value, dict) else None
    raw_directory = value.get("output_directory") if isinstance(value, dict) else None
    unsigned_identity = (
        {key: item for key, item in value.items() if key not in {"identity_sha256", "phase"}}
        if isinstance(value, dict)
        else {}
    )
    if (
        not isinstance(value, dict)
        or canonical_json(value) != data
        or value.get("schema") != SHELL_RECOVERY_SCHEMA
        or value.get("operation") != operation
        or value.get("target") != target
        or value.get("identity_sha256") != sha256_bytes(canonical_json(unsigned_identity))
        or value.get("phase") != "prepared"
        or not isinstance(raw_binding, dict)
        or raw_binding.get("path") != str(binding_path)
        or raw_binding.get("sha256") != binding_sha256
        or not isinstance(raw_binding.get("source_base64"), str)
        or not isinstance(raw_audit, dict)
        or raw_audit.get("path") != str(audit_output)
        or not isinstance(raw_directory, dict)
        or raw_directory
        != {
            "path": str(output_directory.path),
            "device": output_directory.states[-1].st_dev,
            "inode": output_directory.states[-1].st_ino,
            "uid": output_directory.states[-1].st_uid,
            "mode": stat.S_IMODE(output_directory.states[-1].st_mode),
        }
    ):
        raise DrainError("shell recovery journal has the wrong exact route identity")
    try:
        binding_data = base64.b64decode(str(raw_binding["source_base64"]), validate=True)
    except ValueError as error:
        raise DrainError("shell recovery journal contains invalid binding bytes") from error
    if sha256_bytes(binding_data) != binding_sha256:
        raise DrainError("shell recovery journal binding bytes do not match their digest")
    phase = recovery_phase_from_entries(output_directory, journal_path.name, unsigned_identity)
    return phase, binding_data, value


def prepare_shell_recovery(
    output_directory: PinnedDirectory,
    root: Path,
    audit_output: Path,
    binding_path: Path,
    binding_sha256: str,
    binding_data: bytes,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    executor: str,
    executor_pane_id: str,
    operation: str,
    target: str,
    authority: dict[str, str],
    expected_shell: ShellSnapshot,
    audit_data: bytes,
    protected: set[Path],
    task: dict[str, object] | None = None,
    todo: dict[str, object] | None = None,
) -> PreparedShellRecovery:
    output_directory.validate()
    if (
        audit_output.parent != output_directory.path
        or audit_output == root
        or audit_output.is_relative_to(root)
        or audit_output in protected
        or "/" in audit_output.name
        or not audit_output.name
    ):
        raise DrainError("recovery output is not inside its pinned directory")
    staged_audit, journal_path = recovery_paths(audit_output, binding_sha256, operation)
    if staged_audit in protected or journal_path in protected or len({audit_output.name, staged_audit.name, journal_path.name}) != 3:
        raise DrainError("recovery sidecar path overlaps protected state")
    journal_state = pinned_entry_state(output_directory, journal_path.name)
    staged_audit_fd: int | None = None
    staged_audit_state: os.stat_result | None = None
    journal_fd: int | None = None
    held_journal_state: os.stat_result | None = None
    phase_objects: dict[str, HeldRecoveryPhaseObject] = {}
    if journal_state is None:
        if pinned_entry_state(output_directory, audit_output.name) is not None:
            raise DrainError("published recovery output exists without its exact durable phase journal")
        if pinned_entry_state(output_directory, staged_audit.name) is None:
            pinned_write_new_private(output_directory, staged_audit.name, audit_data)
        elif pinned_private_bytes(output_directory, staged_audit.name, "staged shell audit") != audit_data:
            raise DrainError("orphaned staged shell audit has different bytes")
        staged_audit_binding = bind_pinned_audit_object(output_directory, staged_audit.name, audit_data)
        staged_audit_fd, staged_audit_state = validate_pinned_object_binding(output_directory, staged_audit_binding, audit_data, 0o600, "staged shell audit")
        try:
            identity = recovery_identity(
                operation,
                target,
                binding_path,
                binding_sha256,
                binding_data,
                review_path,
                review_sha256,
                consumed_receipt,
                consumed_receipt_sha256,
                executor,
                executor_pane_id,
                authority,
                expected_shell,
                audit_output,
                staged_audit,
                staged_audit_binding,
                output_directory,
                audit_data,
                task,
                todo,
            )
            phase_objects = bind_recovery_phase_objects_held(output_directory, journal_path.name, operation, identity)
            validate_held_staged_audit(output_directory, staged_audit_fd, staged_audit_state, staged_audit_binding, staged_audit.name, audit_data, 1)
            pinned_write_new_private(output_directory, journal_path.name, recovery_journal_bytes(identity, "prepared"))
            journal_entry = pinned_held_object(output_directory, journal_path.name, 0o600, "shell recovery journal")
            if journal_entry is None:
                raise DrainError("shell recovery journal is absent after prepared commit")
            journal_fd, journal_data, held_journal_state = journal_entry
            if journal_data != recovery_journal_bytes(identity, "prepared"):
                raise DrainError("shell recovery journal drifted after prepared commit")
            validate_held_shell_recovery_journal(output_directory, journal_fd, held_journal_state, journal_path.name, identity)
            validate_held_staged_audit(output_directory, staged_audit_fd, staged_audit_state, staged_audit_binding, staged_audit.name, audit_data, 1)
            phase = "prepared"
        except Exception:
            os.close(staged_audit_fd)
            if journal_fd is not None:
                os.close(journal_fd)
            for phase_object in phase_objects.values():
                phase_object.close()
            staged_audit_fd = None
            journal_fd = None
            raise
    else:
        journal_data = pinned_private_bytes(output_directory, journal_path.name, "shell recovery journal")
        try:
            journal_value = json.loads(journal_data)
        except json.JSONDecodeError as error:
            raise DrainError("shell recovery journal is not valid JSON") from error
        if not isinstance(journal_value, dict) or journal_value.get("identity_sha256") != sha256_bytes(canonical_json({key: item for key, item in journal_value.items() if key not in {"identity_sha256", "phase"}})):
            raise DrainError("shell recovery journal has invalid identity bytes")
        saved_identity = {key: item for key, item in journal_value.items() if key not in {"identity_sha256", "phase"}}
        raw_audit = saved_identity.get("audit")
        if not isinstance(raw_audit, dict):
            raise DrainError("shell recovery journal lacks its staged audit binding")
        staged_audit_binding = raw_audit.get("staged_object")
        staged_fd, _ = validate_pinned_object_binding(output_directory, staged_audit_binding, audit_data, 0o600, "staged shell audit")
        os.close(staged_fd)
        identity = recovery_identity(
            operation,
            target,
            binding_path,
            binding_sha256,
            binding_data,
            review_path,
            review_sha256,
            consumed_receipt,
            consumed_receipt_sha256,
            executor,
            executor_pane_id,
            authority,
            expected_shell,
            audit_output,
            staged_audit,
            staged_audit_binding,
            output_directory,
            audit_data,
            task,
            todo,
        )
        identity_without_phase = recovery_core_identity(identity)
        saved_without_phase = recovery_core_identity(saved_identity)
        if identity_without_phase != saved_without_phase or journal_data != recovery_journal_bytes(saved_identity, "prepared"):
            raise DrainError("shell recovery journal does not bind this exact transaction")
        identity = saved_identity
        phase = recovery_phase_from_entries(output_directory, journal_path.name, identity)
        if phase == "complete":
            if (
                pinned_entry_state(output_directory, audit_output.name) is None
                or pinned_private_bytes(output_directory, audit_output.name, "published shell audit") != audit_data
            ):
                raise DrainError("complete shell recovery lacks its exact published audit")
        elif pinned_entry_state(output_directory, audit_output.name) is None:
            if pinned_entry_state(output_directory, staged_audit.name) is None:
                raise DrainError("incomplete shell recovery lost its staged audit")
            if pinned_private_bytes(output_directory, staged_audit.name, "staged shell audit") != audit_data:
                raise DrainError("staged shell audit drifted")
    return PreparedShellRecovery(staged_audit, journal_path, identity, str(phase), staged_audit_fd, staged_audit_state, journal_fd, held_journal_state, phase_objects)


def validate_retained_recovery_phase_object(
    output_directory: PinnedDirectory,
    identity: dict[str, object],
    phase: str,
    raw_record: dict[str, object],
    retained_phase_object: HeldRecoveryPhaseObject,
) -> os.stat_result:
    retained_phase_data, stage_state = read_bounded_fd(
        retained_phase_object.descriptor,
        MAX_PRIVATE_ARTIFACT_BYTES,
        f"retained staged shell recovery {phase} phase",
    )
    try:
        stage_public = os.stat(str(raw_record.get("staged_name", "")), dir_fd=output_directory.descriptor, follow_symlinks=False)
    except OSError as error:
        raise DrainError(f"staged shell recovery {phase} phase identity drifted") from error
    expected_data = recovery_phase_bytes(str(identity["transaction_sha256"]), phase)
    if (
        retained_phase_data != expected_data
        or stable_stat_identity(stage_state) != stable_stat_identity(retained_phase_object.state)
        or stable_stat_identity(stage_public) != stable_stat_identity(stage_state)
        or stage_state.st_nlink != 1
        or not exchange_object_matches(stage_state, raw_record.get("object"))
    ):
        raise DrainError(f"retained shell recovery {phase} phase identity drifted")
    return stage_state


def validate_uncommitted_recovery_phase_objects(
    output_directory: PinnedDirectory,
    identity: dict[str, object],
    held_objects: dict[str, HeldRecoveryPhaseObject],
    phases: set[str],
) -> None:
    records = identity.get("phase_records")
    if not isinstance(records, dict):
        raise DrainError("shell recovery transition lacks pre-staged phase records")
    for phase in phases:
        raw_record = records.get(phase)
        retained_phase_object = held_objects.get(phase)
        if not isinstance(raw_record, dict) or retained_phase_object is None:
            raise DrainError("shell recovery lacks retained future phase descriptor before pane operation")
        validate_retained_recovery_phase_object(output_directory, identity, phase, raw_record, retained_phase_object)


def validate_committed_recovery_phase_object(
    output_directory: PinnedDirectory,
    identity: dict[str, object],
    phase: str,
    raw_record: dict[str, object],
    retained_phase_object: HeldRecoveryPhaseObject,
) -> None:
    data, held = read_bounded_fd(
        retained_phase_object.descriptor,
        MAX_PRIVATE_ARTIFACT_BYTES,
        f"retained committed shell recovery {phase} phase",
    )
    expected_data = recovery_phase_bytes(str(identity["transaction_sha256"]), phase)
    try:
        staged_public = os.stat(str(raw_record.get("staged_name", "")), dir_fd=output_directory.descriptor, follow_symlinks=False)
        committed_public = os.stat(str(raw_record.get("committed_name", "")), dir_fd=output_directory.descriptor, follow_symlinks=False)
    except OSError as error:
        raise DrainError(f"committed shell recovery {phase} phase identity drifted before pane operation") from error
    final_data, final_held = read_bounded_fd(
        retained_phase_object.descriptor,
        MAX_PRIVATE_ARTIFACT_BYTES,
        f"retained committed shell recovery {phase} phase",
    )
    if (
        data != expected_data
        or final_data != expected_data
        or link_bridge_identity(held) != link_bridge_identity(retained_phase_object.state)
        or link_bridge_identity(final_held) != link_bridge_identity(retained_phase_object.state)
        or stable_stat_identity(staged_public) != stable_stat_identity(held)
        or stable_stat_identity(committed_public) != stable_stat_identity(held)
        or held.st_nlink != 2
        or final_held.st_nlink != 2
        or not exchange_object_matches(final_held, raw_record.get("object"))
    ):
        raise DrainError(f"committed shell recovery {phase} phase identity drifted before pane operation")


def validate_final_pre_removal_recovery_state(
    output_directory: PinnedDirectory,
    journal_path: Path,
    identity: dict[str, object],
    journal_fd: int | None,
    journal_state: os.stat_result | None,
    held_objects: dict[str, HeldRecoveryPhaseObject],
) -> None:
    output_directory.validate()
    if journal_fd is None or journal_state is None:
        raise DrainError("shell recovery lacks retained journal descriptor before pane operation")
    validate_held_shell_recovery_journal(output_directory, journal_fd, journal_state, journal_path.name, identity)
    if recovery_phase_from_entries(output_directory, journal_path.name, identity) != "kill-started":
        raise DrainError("shell recovery phase drifted before pane operation")
    records = identity.get("phase_records")
    if not isinstance(records, dict):
        raise DrainError("shell recovery transition lacks pre-staged phase records")
    kill_started_record = records.get("kill-started")
    kill_started = held_objects.get("kill-started")
    if not isinstance(kill_started_record, dict) or kill_started is None:
        raise DrainError("shell recovery lacks retained kill-started descriptor before pane operation")
    validate_committed_recovery_phase_object(output_directory, identity, "kill-started", kill_started_record, kill_started)
    validate_uncommitted_recovery_phase_objects(output_directory, identity, held_objects, {"pane-removed", "complete"})
    validate_held_shell_recovery_journal(output_directory, journal_fd, journal_state, journal_path.name, identity)
    if recovery_phase_from_entries(output_directory, journal_path.name, identity) != "kill-started":
        raise DrainError("shell recovery phase drifted during pre-removal validation")
    output_directory.validate()
    raise DrainError("taskless shell removal requires sealed phase-publication generation proof before pane operation")


def transition_shell_recovery(
    output_directory: PinnedDirectory,
    journal_path: Path,
    identity: dict[str, object],
    expected_phase: str,
    next_phase: str,
    audit_data: bytes | None = None,
    retained_staged_audit_fd: int | None = None,
    retained_staged_audit_state: os.stat_result | None = None,
    retained_phase_object: HeldRecoveryPhaseObject | None = None,
) -> str:
    operation = identity.get("operation")
    if not isinstance(operation, str):
        raise DrainError("shell recovery identity lacks its phase-graph operation")
    phases = recovery_phases(operation)
    try:
        expected_index = phases.index(expected_phase)
    except ValueError as error:
        raise DrainError("expected shell recovery phase is outside its phase graph") from error
    if expected_index + 1 >= len(phases) or phases[expected_index + 1] != next_phase:
        raise DrainError("shell recovery transition is not the exact next phase")
    if recovery_phase_from_entries(output_directory, journal_path.name, identity) != expected_phase:
        raise DrainError("shell recovery phase drifted before append-only transition")
    raw_audit = identity.get("audit")
    if isinstance(raw_audit, dict) and "staged_object" in raw_audit:
        staged_path = raw_audit.get("staged_path")
        if (
            audit_data is None
            or retained_staged_audit_fd is None
            or retained_staged_audit_state is None
            or not isinstance(staged_path, str)
        ):
            raise DrainError("shell recovery lacks retained staged audit descriptor before phase transition")
        validate_held_staged_audit(
            output_directory,
            retained_staged_audit_fd,
            retained_staged_audit_state,
            raw_audit.get("staged_object"),
            Path(staged_path).name,
            audit_data,
            2 if next_phase == "complete" else 1,
        )
    records = identity.get("phase_records")
    if not isinstance(records, dict):
        raise DrainError("shell recovery transition lacks pre-staged phase records")
    raw_record = records.get(next_phase)
    if not isinstance(raw_record, dict):
        raise DrainError("shell recovery transition lacks its pre-staged next phase")
    committed_name = str(raw_record.get("committed_name", ""))
    if retained_phase_object is None and isinstance(raw_audit, dict) and "staged_object" in raw_audit:
        raise DrainError("shell recovery lacks retained future phase descriptor before phase transition")
    if retained_phase_object is not None:
        stage_state = validate_retained_recovery_phase_object(output_directory, identity, next_phase, raw_record, retained_phase_object)
        stage_fd = retained_phase_object.descriptor
    else:
        stage_fd, _, stage_state = held_recovery_phase_record(
            output_directory,
            str(identity["transaction_sha256"]),
            next_phase,
            raw_record,
            "staged_name",
            f"staged shell recovery {next_phase} phase",
        )
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    try:
        if linkat(stage_fd, b"", output_directory.descriptor, committed_name.encode(), AT_EMPTY_PATH) != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.ENOENT:
                if linkat(AT_FDCWD, f"/proc/self/fd/{stage_fd}".encode(), output_directory.descriptor, committed_name.encode(), AT_SYMLINK_FOLLOW) != 0:
                    error_number = ctypes.get_errno()
                else:
                    error_number = 0
            if error_number == errno.EEXIST:
                committed_fd, _, committed_state = held_recovery_phase_record(
                    output_directory,
                    str(identity["transaction_sha256"]),
                    next_phase,
                    raw_record,
                    "committed_name",
                    f"committed shell recovery {next_phase} phase",
                )
                os.close(committed_fd)
                if exchange_object_identity(committed_state) != exchange_object_identity(stage_state):
                    raise DrainError("shell recovery committed phase collides with foreign bytes")
            elif error_number:
                raise OSError(error_number, os.strerror(error_number), committed_name)
        os.fsync(output_directory.descriptor)
        committed = os.stat(committed_name, dir_fd=output_directory.descriptor, follow_symlinks=False)
        if exchange_object_identity(committed) != exchange_object_identity(stage_state):
            raise DrainError("shell recovery committed phase is not the held staged object")
        if retained_phase_object is not None:
            validate_committed_recovery_phase_object(output_directory, identity, next_phase, raw_record, retained_phase_object)
        if recovery_phase_from_entries(output_directory, journal_path.name, identity) != next_phase:
            raise DrainError("shell recovery phase did not durably reach its exact next state")
        return next_phase
    finally:
        if retained_phase_object is None:
            os.close(stage_fd)


def cas_private_file(path: Path, expected: bytes, replacement: bytes) -> None:
    atomic_replace_task(path.parent, path, expected, replacement)


def bound_shell_processes_exited(expected: ShellSnapshot) -> bool:
    if expected.root is None or expected.foreground is None:
        return False
    for process in (expected.root, expected.foreground):
        observed = process_start_ticks(process.pid)
        if observed == process.start_ticks:
            return False
        if observed is not None:
            continue
        try:
            descriptor = pidfd_open(process.pid)
        except OSError as error:
            if error.errno == 3:
                continue
            raise DrainError("cannot prove exact bound shell process absence") from error
        try:
            observed = process_start_ticks(process.pid)
        finally:
            os.close(descriptor)
        if observed == process.start_ticks:
            return False
        if observed is None:
            raise DrainError("bound shell process generation is unreadable rather than proven exited")
    return True


def recovery_pane_presence(target: str, expected_pane_id: str) -> str:
    result = tmux_result(
        ["list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}|#{pane_id}"],
        f"authoritative recovery pane inventory for {target}",
    )
    if result.returncode != 0:
        raise DrainError("cannot prove authoritative pane presence for recovery")
    try:
        canonical = canonical_target(target)
        rows = [line.partition("|") for line in result.stdout.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, DrainError) as error:
        raise DrainError("recovery pane inventory is invalid") from error
    target_panes: list[str] = []
    pane_targets: list[str] = []
    for resolved, separator, pane_id in rows:
        if not separator or re.fullmatch(r"%\d+", pane_id) is None:
            raise DrainError("recovery pane inventory contains a malformed row")
        try:
            resolved_canonical = canonical_target(resolved)
        except DrainError as error:
            raise DrainError("recovery pane inventory contains a malformed target") from error
        if resolved_canonical == canonical:
            target_panes.append(pane_id)
        if pane_id == expected_pane_id:
            pane_targets.append(resolved_canonical)
    if len(target_panes) > 1 or len(pane_targets) > 1:
        raise DrainError("recovery pane inventory contains duplicate identities")
    if target_panes == [expected_pane_id] and pane_targets == [canonical]:
        return "present"
    if target_panes or pane_targets:
        raise DrainError("symbolic target or exact pane rebound during recovery")
    return "absent"


def shell_recovery_state(target: str, expected: ShellSnapshot, require_empty_transcript: bool) -> str:
    presence = recovery_pane_presence(target, expected.pane_id)
    if presence == "present":
        if shell_snapshot(target, require_empty_transcript=require_empty_transcript) != expected:
            raise DrainError("bound shell pane or process generation drifted during recovery")
        return "present"
    if not bound_shell_processes_exited(expected):
        raise ShellRemovalPartialError("bound pane is absent but an exact shell process generation still survives")
    return "absent"


def publish_or_validate_staged_audit(
    output_directory: PinnedDirectory,
    staged: Path,
    output: Path,
    audit_data: bytes,
    staged_audit_binding: object,
    held_staged_fd: int,
    held_staged_state: os.stat_result,
) -> HeldPublishedAudit:
    output_fd = -1
    staged_fd = os.dup(held_staged_fd)
    try:
        output_entry = pinned_held_entry(output_directory, output.name, "published shell audit")
        validate_held_staged_audit(
            output_directory,
            staged_fd,
            held_staged_state,
            staged_audit_binding,
            staged.name,
            audit_data,
            1 if output_entry is None else 2,
        )
        if output_entry is None:
            link_held_staged_audit(output_directory, staged_fd, output.name)
            output_entry = pinned_held_entry(output_directory, output.name, "published shell audit")
            if output_entry is None:
                raise DrainError("published shell audit disappeared after publication")
        output_fd, output_data, output_state = output_entry
        staged_data, staged_held = read_bounded_fd(staged_fd, MAX_PRIVATE_ARTIFACT_BYTES, "retained staged shell audit")
        output_public = os.stat(output.name, dir_fd=output_directory.descriptor, follow_symlinks=False)
        staged_public = os.stat(staged.name, dir_fd=output_directory.descriptor, follow_symlinks=False)
        output_held = os.fstat(output_fd)
        if output_data != audit_data:
            raise DrainError("shell audit output collides with different bytes")
        if staged_data != audit_data:
            raise DrainError("retained staged shell audit conflicts with the published output")
        if (
            audit_link_identity(output_state) != audit_link_identity(staged_held)
            or audit_link_identity(output_public) != audit_link_identity(output_state)
            or audit_link_identity(staged_public) != audit_link_identity(staged_held)
            or audit_link_identity(output_held) != audit_link_identity(output_state)
            or exchange_object_identity(staged_held) != exchange_object_identity(held_staged_state)
            or output_held.st_nlink != 2
            or staged_held.st_nlink != 2
            or not isinstance(staged_audit_binding, dict)
            or not exchange_object_matches(staged_held, staged_audit_binding.get("object"))
        ):
            raise DrainError("published shell audit is not the exact held staged object")
        held = HeldPublishedAudit(output_fd, staged_fd, output_state, staged_held)
        output_fd = -1
        staged_fd = -1
        return held
    finally:
        if staged_fd >= 0:
            os.close(staged_fd)
        if output_fd >= 0:
            os.close(output_fd)


def revalidate_held_published_audit(
    output_directory: PinnedDirectory,
    held: HeldPublishedAudit,
    staged_name: str,
    output_name: str,
) -> None:
    output_directory.validate()
    states = (
        os.fstat(held.published_fd),
        os.fstat(held.staged_fd),
        os.stat(output_name, dir_fd=output_directory.descriptor, follow_symlinks=False),
        os.stat(staged_name, dir_fd=output_directory.descriptor, follow_symlinks=False),
        held.published_state,
        held.staged_state,
    )
    first = audit_link_identity(states[0])
    if any(audit_link_identity(state) != first for state in states[1:]):
        raise DrainError("published shell audit identity drifted before completion")


def link_held_staged_audit(directory: PinnedDirectory, staged_fd: int, output_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    linkat = libc.linkat
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    if linkat(staged_fd, b"", directory.descriptor, output_name.encode(), AT_EMPTY_PATH) != 0:
        error_number = ctypes.get_errno()
        if error_number != errno.ENOENT:
            raise OSError(error_number, os.strerror(error_number), output_name)
        if linkat(AT_FDCWD, f"/proc/self/fd/{staged_fd}".encode(), directory.descriptor, output_name.encode(), AT_SYMLINK_FOLLOW) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), output_name)
    os.fsync(directory.descriptor)
    directory.validate()


def close_taskless_shell(
    root: Path,
    binding_path: Path,
    binding_sha256: str,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    audit_output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    audit_output = audit_output.absolute()
    with (
        root_membership_lock(root),
        task_target_lock(root, TASKLESS_SHELL_TARGET),
        pinned_directory(audit_output.parent) as output_directory,
    ):
        probe = probe_shell_recovery(
            output_directory,
            audit_output,
            binding_path,
            binding_sha256,
            "taskless-empty-shell",
            TASKLESS_SHELL_TARGET,
        )
        binding_data = (
            private_file_bytes(binding_path, binding_sha256, "shell binding")
            if probe is None
            else probe[1]
        )
        binding, expected_shell = parse_shell_binding(binding_data, "taskless-empty-shell", TASKLESS_SHELL_TARGET)
        executor_pane_id = authenticate_executor(executor)
        validate_binding_route(binding, review_sha256, executor, executor_pane_id)
        authority = bound_namespace_authority(binding)
        phase_hint = probe[0] if probe is not None else ""
        presence = recovery_pane_presence(TASKLESS_SHELL_TARGET, expected_shell.pane_id) if probe is not None else "unchecked"
        postkill_recovery = probe is not None and phase_hint != "prepared" and presence == "absent"
        if not postkill_recovery:
            if namespace_authority(root) != authority:
                raise DrainError("Source-1261 authority drifted before taskless-shell reconciliation")
            review = reconciliation_preflight(
                root,
                binding_path,
                review_path,
                review_sha256,
                consumed_receipt,
                consumed_receipt_sha256,
                audit_output,
                executor,
                allow_existing_output=True,
            )
            if private_file_bytes(binding_path, binding_sha256, "shell binding") != binding_data:
                raise DrainError("external shell binding drifted before the kill commit point")
        else:
            reviewer = binding.get("reviewer")
            if not isinstance(reviewer, str):
                raise DrainError("recovery binding lacks its exact reviewed actor")
            review = {"reviewer": reviewer}
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted before taskless-shell removal")
        if not postkill_recovery:
            matches = task_records_for_target(root, TASKLESS_SHELL_TARGET)
            if matches or binding.get("task_record_count") != 0:
                raise DrainError("taskless-shell proof no longer has zero task records")
        if presence == "unchecked":
            presence = recovery_pane_presence(TASKLESS_SHELL_TARGET, expected_shell.pane_id)
        audit: dict[str, object] = {
            "schema": TASKLESS_SHELL_AUDIT_SCHEMA,
            "target": TASKLESS_SHELL_TARGET,
            "removed_pane_id": expected_shell.pane_id,
            "binding_sha256": binding_sha256,
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "task_record_count": 0,
            "human_authority": authority,
            "email_policy": "suppressed",
        }
        audit_data = canonical_json(audit)
        protected = {
            binding_path.resolve(),
            review_path.resolve(),
            consumed_receipt.resolve(),
            TRUSTED_ROOT / "manager_mail" / AUTHORITY_NAME,
        }
        if not postkill_recovery:
            protected.update(root.rglob("*.md"))
        prepared_recovery = prepare_shell_recovery(
            output_directory,
            root,
            audit_output,
            binding_path,
            binding_sha256,
            binding_data,
            review_path,
            review_sha256,
            consumed_receipt,
            consumed_receipt_sha256,
            executor,
            executor_pane_id,
            "taskless-empty-shell",
            TASKLESS_SHELL_TARGET,
            authority,
            expected_shell,
            audit_data,
            protected,
        )
        try:
            staged_audit = prepared_recovery.staged_audit
            journal_path = prepared_recovery.journal_path
            recovery = prepared_recovery.identity
            phase = prepared_recovery.phase
            raw_audit = recovery.get("audit")
            if not isinstance(raw_audit, dict):
                raise DrainError("shell recovery journal lacks its audit identity")
            staged_audit_binding = raw_audit.get("staged_object")
            if phase == "prepared" and prepared_recovery.staged_audit_fd is None:
                raise DrainError("prepared shell recovery lacks retained staged audit descriptor")
            if prepared_recovery.staged_audit_fd is not None and prepared_recovery.staged_audit_state is not None:
                validate_held_staged_audit(
                    output_directory,
                    prepared_recovery.staged_audit_fd,
                    prepared_recovery.staged_audit_state,
                    staged_audit_binding,
                    staged_audit.name,
                    audit_data,
                    1,
                )
            if phase == "complete":
                if shell_recovery_state(TASKLESS_SHELL_TARGET, expected_shell, True) != "absent":
                    raise DrainError("complete recovery journal conflicts with a live exact pane")
                if prepared_recovery.staged_audit_fd is None or prepared_recovery.staged_audit_state is None:
                    raise DrainError("complete shell recovery lacks retained staged audit descriptor")
                held_audit = publish_or_validate_staged_audit(
                    output_directory,
                    staged_audit,
                    audit_output,
                    audit_data,
                    staged_audit_binding,
                    prepared_recovery.staged_audit_fd,
                    prepared_recovery.staged_audit_state,
                )
                try:
                    return audit
                finally:
                    held_audit.close()
            if phase in {"prepared", "kill-started", "pane-removed"} and (
                prepared_recovery.staged_audit_fd is None or prepared_recovery.staged_audit_state is None
            ):
                raise DrainError("incomplete shell recovery lacks retained staged audit descriptor before pane operation")
            state = shell_recovery_state(TASKLESS_SHELL_TARGET, expected_shell, True)
            if phase == "prepared" and state == "absent":
                raise DrainError("taskless pane is absent without a durable kill-started phase")
            if phase in {"prepared", "kill-started"} and state == "present":
                if not postkill_recovery and namespace_authority(root) != authority:
                    raise DrainError("Source-1261 authority drifted before taskless-shell removal")
                if phase == "prepared":
                    phase = transition_shell_recovery(
                        output_directory,
                        journal_path,
                        recovery,
                        phase,
                        "kill-started",
                        audit_data,
                        prepared_recovery.staged_audit_fd,
                        prepared_recovery.staged_audit_state,
                        prepared_recovery.phase_objects.get("kill-started"),
                    )
                    if prepared_recovery.staged_audit_fd is not None and prepared_recovery.staged_audit_state is not None:
                        validate_held_staged_audit(
                            output_directory,
                            prepared_recovery.staged_audit_fd,
                            prepared_recovery.staged_audit_state,
                            staged_audit_binding,
                            staged_audit.name,
                            audit_data,
                            1,
                        )
                validate_final_pre_removal_recovery_state(
                    output_directory,
                    journal_path,
                    recovery,
                    prepared_recovery.journal_fd,
                    prepared_recovery.journal_state,
                    prepared_recovery.phase_objects,
                )
                try:
                    guarded_remove_shell(TASKLESS_SHELL_TARGET, expected_shell, require_empty_transcript=True)
                except ShellRemovalPartialError:
                    raise
                state = shell_recovery_state(TASKLESS_SHELL_TARGET, expected_shell, True)
            if phase == "kill-started" and state == "absent":
                if not postkill_recovery and namespace_authority(root) != authority:
                    raise ShellRemovalPartialError("taskless pane is gone but Source-1261 authority drifted during reconciliation")
                phase = transition_shell_recovery(
                    output_directory,
                    journal_path,
                    recovery,
                    phase,
                    "pane-removed",
                    audit_data,
                    prepared_recovery.staged_audit_fd,
                    prepared_recovery.staged_audit_state,
                    prepared_recovery.phase_objects.get("pane-removed"),
                )
            if phase != "pane-removed":
                raise DrainError(f"taskless recovery cannot proceed from phase {phase} with pane {state}")
            if shell_recovery_state(TASKLESS_SHELL_TARGET, expected_shell, True) != "absent":
                raise DrainError("taskless recovery cannot publish while the exact pane remains")
            if not postkill_recovery and namespace_authority(root) != authority:
                raise ShellRemovalPartialError("taskless pane is gone but Source-1261 authority drifted before audit publication")
            if prepared_recovery.staged_audit_fd is None or prepared_recovery.staged_audit_state is None:
                raise DrainError("taskless audit publication lacks retained staged audit descriptor")
            held_audit = publish_or_validate_staged_audit(
                output_directory,
                staged_audit,
                audit_output,
                audit_data,
                staged_audit_binding,
                prepared_recovery.staged_audit_fd,
                prepared_recovery.staged_audit_state,
            )
            try:
                revalidate_held_published_audit(output_directory, held_audit, staged_audit.name, audit_output.name)
                _ = transition_shell_recovery(
                    output_directory,
                    journal_path,
                    recovery,
                    phase,
                    "complete",
                    audit_data,
                    held_audit.staged_fd,
                    held_audit.staged_state,
                    prepared_recovery.phase_objects.get("complete"),
                )
                revalidate_held_published_audit(output_directory, held_audit, staged_audit.name, audit_output.name)
                return audit
            finally:
                held_audit.close()
        finally:
            prepared_recovery.close()


def binding_entry(binding: dict[str, object], name: str, expected_path: str) -> tuple[bytes, bytes]:
    raw = binding.get(name)
    if not isinstance(raw, dict) or raw.get("path") != expected_path or not isinstance(raw.get("sha256"), str):
        raise DrainError(f"lifecycle binding has invalid {name} identity")
    source = raw.get("source_base64")
    replacement = raw.get("replacement_base64")
    if not isinstance(source, str) or not isinstance(replacement, str):
        raise DrainError(f"lifecycle binding lacks exact {name} bytes")
    try:
        source_data = base64.b64decode(source, validate=True)
        replacement_data = base64.b64decode(replacement, validate=True)
    except ValueError as error:
        raise DrainError(f"lifecycle binding has invalid {name} byte encoding") from error
    if sha256_bytes(source_data) != raw["sha256"]:
        raise DrainError(f"lifecycle binding {name} digest mismatch")
    return source_data, replacement_data


def parse_absent_manager_binding(data: bytes) -> dict[str, object]:
    try:
        binding = json.loads(data)
    except json.JSONDecodeError as error:
        raise DrainError("absent-manager binding is not valid JSON") from error
    if (
        not isinstance(binding, dict)
        or binding.get("schema") != ABSENT_MANAGER_BINDING_SCHEMA
        or binding.get("kind") != "absent-long-running-manager-history"
        or not isinstance(binding.get("target"), str)
        or not isinstance(binding.get("human_authority"), dict)
        or binding.get("email_policy") != "suppressed"
        or binding.get("pane_policy") != "no-tmux-mutation"
        or binding.get("pending_policy") != "preserved"
        or binding.get("todo_policy") != "current-targetless-custody"
    ):
        raise DrainError("absent-manager binding has the wrong exact operation identity")
    return binding


def load_absent_manager_binding(path: Path, expected_sha256: str) -> dict[str, object]:
    return parse_absent_manager_binding(private_file_bytes(path, expected_sha256, "absent-manager binding"))


def rollback_absent_manager_history(
    root: Path,
    task: Path,
    todo: Path,
    task_source: bytes,
    task_replacement: bytes,
    todo_source: bytes,
    todo_replacement: bytes,
) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    blocked: list[str] = []
    for path, source, replacement, label in (
        (todo, todo_source, todo_replacement, "TODO.md"),
        (task, task_source, task_replacement, str(task.relative_to(root))),
    ):
        try:
            current = task_bytes_no_follow(root, path)
            if current == replacement:
                atomic_replace_task(root, path, replacement, source)
                restored.append(label)
            elif current != source:
                blocked.append(label)
        except Exception:
            blocked.append(label)
    return restored, sorted(set(blocked))


def close_absent_manager_history(
    root: Path,
    binding_path: Path,
    binding_sha256: str,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    audit_output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    binding = load_absent_manager_binding(binding_path, binding_sha256)
    target = str(binding["target"])
    _ = reconciliation_preflight(root, binding_path, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, audit_output, executor)
    executor_pane_id = authenticate_executor(executor)
    validate_binding_route(binding, review_sha256, executor, executor_pane_id)
    raw_task = binding.get("task")
    if not isinstance(raw_task, dict) or not isinstance(raw_task.get("path"), str):
        raise DrainError("absent-manager binding lacks its task path")
    task_data, task_replacement = binding_entry(binding, "task", str(raw_task["path"]))
    todo_data, todo_replacement = binding_entry(binding, "todo", "TODO.md")
    task = (root / str(raw_task["path"])).resolve()
    todo = root / "TODO.md"
    if not task.is_relative_to(root) or task == todo:
        raise DrainError("absent-manager binding task path is unsafe")
    raw_metadata = binding.get("metadata")
    if not isinstance(raw_metadata, dict) or raw_metadata.get("runat") != target or raw_metadata.get("is_manager") is not True:
        raise DrainError("absent-manager binding metadata is invalid")
    with root_membership_lock(root), task_target_lock(root, target), ExitStack() as locks:
        for locked_path in sorted({task, todo}, key=str):
            locks.enter_context(task_file_lock(locked_path))
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted before absent-manager reconciliation")
        if task_bytes_no_follow(root, task) != task_data or task_bytes_no_follow(root, todo) != todo_data:
            raise DrainError("absent-manager task or TODO bytes drifted before reconciliation")
        derived_task, derived_todo, absence, owner_generation, metadata = require_absent_manager_source_state(
            root,
            task,
            task_data,
            todo_data,
            target,
            str(raw_task["sha256"]),
        )
        if derived_task != task_replacement or derived_todo != todo_replacement:
            raise DrainError("absent-manager binding does not contain the exact supported replacements")
        if binding.get("target_absence") != absence or binding.get("owner_generation") != owner_generation or binding.get("metadata") != metadata:
            raise DrainError("absent-manager ownership or target evidence drifted")
        if namespace_authority(root) != bound_namespace_authority(binding):
            raise DrainError("Source-1261 authority drifted before absent-manager reconciliation")
        validate_dirty_snapshot(root, (task, todo), binding.get("dirty_manifest"))
        raise DrainError(ABSENT_MANAGER_PUBLIC_MUTATION_BLOCKER)


def exact_file_transition_state(current: bytes, source: bytes, replacement: bytes, label: str) -> str:
    if source == replacement and current == source:
        return "unchanged"
    if current == source:
        return "source"
    if current == replacement:
        return "replacement"
    raise DrainError(f"{label} bytes match neither exact transaction state")


def lifecycle_exchange_slot_name(path: Path, binding_sha256: str) -> str:
    token = sha256_bytes(f"completed-task-shell\0{binding_sha256}\0{path.name}".encode())[:24]
    return f".{path.name}.namespace-drain-{token}.replacement"


def reject_unsupported_completed_shell_lifecycle() -> None:
    raise DrainError("completed-shell lifecycle mutation is unsupported before pane removal: task/TODO RENAME_EXCHANGE cannot prove descriptor-bound mutation against same-UID name swaps")


def close_completed_shell(
    root: Path,
    binding_path: Path,
    binding_sha256: str,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    audit_output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    audit_output = audit_output.absolute()
    with (
        root_membership_lock(root),
        task_target_lock(root, COMPLETED_SHELL_TARGET),
        pinned_directory(audit_output.parent) as output_directory,
        ExitStack() as locks,
    ):
        probe = probe_shell_recovery(
            output_directory,
            audit_output,
            binding_path,
            binding_sha256,
            "completed-task-shell",
            COMPLETED_SHELL_TARGET,
        )
        binding_data = (
            private_file_bytes(binding_path, binding_sha256, "shell binding")
            if probe is None
            else probe[1]
        )
        binding, expected_shell = parse_shell_binding(binding_data, "completed-task-shell", COMPLETED_SHELL_TARGET)
        executor_pane_id = authenticate_executor(executor)
        validate_binding_route(binding, review_sha256, executor, executor_pane_id)
        authority = bound_namespace_authority(binding)
        phase_hint = probe[0] if probe is not None else ""
        presence = recovery_pane_presence(COMPLETED_SHELL_TARGET, expected_shell.pane_id) if probe is not None else "unchecked"
        postkill_recovery = probe is not None and phase_hint != "prepared" and presence == "absent"
        if not postkill_recovery:
            if namespace_authority(root) != authority:
                raise DrainError("Source-1261 authority drifted before completed-shell reconciliation")
            review = reconciliation_preflight(
                root,
                binding_path,
                review_path,
                review_sha256,
                consumed_receipt,
                consumed_receipt_sha256,
                audit_output,
                executor,
                allow_existing_output=True,
            )
            if private_file_bytes(binding_path, binding_sha256, "shell binding") != binding_data:
                raise DrainError("external shell binding drifted before the kill commit point")
        else:
            reviewer = binding.get("reviewer")
            if not isinstance(reviewer, str):
                raise DrainError("recovery binding lacks its exact reviewed actor")
            review = {"reviewer": reviewer}
        task = root / COMPLETED_SHELL_TASK
        todo = root / "TODO.md"
        task_data, task_replacement = binding_entry(binding, "task", COMPLETED_SHELL_TASK)
        todo_data, todo_replacement = binding_entry(binding, "todo", "TODO.md")
        raw_manager = binding.get("manager")
        raw_receipt = binding.get("completion_receipt")
        dirty = binding.get("dirty_manifest")
        if not isinstance(raw_manager, dict) or not isinstance(raw_receipt, dict):
            raise DrainError("completed-shell binding lacks manager or receipt evidence")
        manager_task = raw_manager.get("task")
        if not isinstance(manager_task, str):
            raise DrainError("completed-shell binding lacks its manager task path")
        manager_relative = Path(manager_task)
        if manager_relative.is_absolute() or ".." in manager_relative.parts or not manager_relative.parts:
            raise DrainError("completed-shell binding has an unsafe manager task path")
        manager_path = root / manager_relative
        if manager_path in {task, todo}:
            raise DrainError("completed-shell binding overlaps its lifecycle files")
        receipt_path = Path(str(raw_receipt.get("path", "")))
        receipt_sha256 = str(raw_receipt.get("sha256", ""))
        lock_paths = (task, todo) if postkill_recovery else (task, todo, manager_path)
        for locked_path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(locked_path))
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted before completed-shell removal")
        current_task = task_bytes_no_follow(root, task)
        current_todo = task_bytes_no_follow(root, todo)
        task_state = exact_file_transition_state(current_task, task_data, task_replacement, "completed-shell task")
        todo_state = exact_file_transition_state(current_todo, todo_data, todo_replacement, "completed-shell TODO")
        text = task_data.decode("utf-8")
        todo_text = todo_data.decode("utf-8")
        prepared_at = binding.get("prepared_at")
        bound_close_note = binding.get("close_note")
        if not isinstance(prepared_at, str) or not isinstance(bound_close_note, str):
            raise DrainError("completed-shell binding lacks its preparation time or close note")
        try:
            created_at = datetime.fromisoformat(prepared_at)
        except ValueError as error:
            raise DrainError("completed-shell binding has an invalid preparation time") from error
        derived_task, derived_todo = completed_task_replacements(root, task, text, todo_text, created_at, bound_close_note)
        if task_replacement != derived_task.encode() or todo_replacement != derived_todo.encode():
            raise DrainError("completed-shell replacement bytes are not the supported lifecycle transition")
        metadata = parse_task_metadata(text, root)
        if metadata is None or raw_manager.get("target") != metadata.managerat:
            raise DrainError("completed-shell manager binding drifted")
        if not postkill_recovery:
            owner_path, owner_data = active_manager_owner(root, metadata.managerat)
            if (
                owner_path != manager_path
                or raw_manager.get("task") != str(owner_path.relative_to(root))
                or raw_manager.get("task_sha256") != sha256_bytes(owner_data)
                or raw_manager.get("pane_id") != exact_pane_id(metadata.managerat)
            ):
                raise DrainError("completed-shell active owner identity drifted")
        if not postkill_recovery:
            active_target_records = task_records_for_target(root, COMPLETED_SHELL_TARGET, active_only=True)
            expected_active = (task.resolve(),) if task_state == "source" else ()
            if active_target_records != expected_active:
                raise DrainError("completed shell active-task ownership does not match its exact lifecycle bytes")
        if not postkill_recovery:
            _ = validate_completion_receipt(root, task, text, receipt_path, receipt_sha256)
            validate_dirty_snapshot(root, (task, todo), dirty)
        task_metadata = parse_task_metadata(task_replacement.decode("utf-8"), root)
        if task_metadata is None or task_metadata.status != "done":
            raise DrainError("completed-shell task replacement is not done")
        audit: dict[str, object] = {
            "schema": COMPLETED_SHELL_AUDIT_SCHEMA,
            "target": COMPLETED_SHELL_TARGET,
            "removed_pane_id": expected_shell.pane_id,
            "task": COMPLETED_SHELL_TASK,
            "task_before_sha256": sha256_bytes(task_data),
            "task_after_sha256": sha256_bytes(task_replacement),
            "todo_before_sha256": sha256_bytes(todo_data),
            "todo_after_sha256": sha256_bytes(todo_replacement),
            "completion_receipt_sha256": receipt_sha256,
            "binding_sha256": binding_sha256,
            "code_review_sha256": review_sha256,
            "reviewer": review["reviewer"],
            "executor": executor,
            "human_authority": authority,
            "email_policy": "suppressed",
        }
        audit_data = canonical_json(audit)
        if probe is None:
            if task_state != "source" or todo_state != "source":
                raise DrainError("fresh completed-shell recovery requires exact source lifecycle bytes before prepared")
            reject_unsupported_completed_shell_lifecycle()
        transition_task: dict[str, object] = {
            "path": COMPLETED_SHELL_TASK,
            "source_sha256": sha256_bytes(task_data),
            "replacement_sha256": sha256_bytes(task_replacement),
            "source_base64": base64.b64encode(task_data).decode("ascii"),
            "replacement_base64": base64.b64encode(task_replacement).decode("ascii"),
        }
        transition_todo: dict[str, object] = {
            "path": "TODO.md",
            "source_sha256": sha256_bytes(todo_data),
            "replacement_sha256": sha256_bytes(todo_replacement),
            "source_base64": base64.b64encode(todo_data).decode("ascii"),
            "replacement_base64": base64.b64encode(todo_replacement).decode("ascii"),
        }
        task_exchange_slot = lifecycle_exchange_slot_name(task, binding_sha256)
        todo_exchange_slot = lifecycle_exchange_slot_name(todo, binding_sha256)
        transition_task["exchange_slot"] = task_exchange_slot
        transition_todo["exchange_slot"] = todo_exchange_slot
        transition_task["directory"] = lifecycle_parent_identity(root, task)
        transition_todo["directory"] = lifecycle_parent_identity(root, todo)
        if probe is not None and isinstance(probe[2].get("task"), dict) and isinstance(probe[2].get("todo"), dict):
            transition_task = probe[2]["task"]
            transition_todo = probe[2]["todo"]
            transition_task = validate_lifecycle_binding(transition_task, root, task, task_data, task_replacement, task_exchange_slot)
            transition_todo = validate_lifecycle_binding(transition_todo, root, todo, todo_data, todo_replacement, todo_exchange_slot)
        elif task_state != "source" or todo_state != "source":
            raise DrainError("fresh completed-shell recovery requires exact source lifecycle bytes before prepared")
        held_task: HeldLifecycleExchange | None = None
        held_todo: HeldLifecycleExchange | None = None
        protected = {
            binding_path.resolve(),
            review_path.resolve(),
            consumed_receipt.resolve(),
            receipt_path,
            task,
            todo,
            manager_path,
            TRUSTED_ROOT / "manager_mail" / AUTHORITY_NAME,
        }
        if not postkill_recovery:
            protected.update(root.rglob("*.md"))
        prepared_recovery = prepare_shell_recovery(
            output_directory,
            root,
            audit_output.absolute(),
            binding_path,
            binding_sha256,
            binding_data,
            review_path,
            review_sha256,
            consumed_receipt,
            consumed_receipt_sha256,
            executor,
            executor_pane_id,
            "completed-task-shell",
            COMPLETED_SHELL_TARGET,
            authority,
            expected_shell,
            audit_data,
            protected,
            transition_task,
            transition_todo,
        )
        staged_audit = prepared_recovery.staged_audit
        journal_path = prepared_recovery.journal_path
        recovery = prepared_recovery.identity
        phase = prepared_recovery.phase
        raw_audit = recovery.get("audit")
        if not isinstance(raw_audit, dict):
            raise DrainError("shell recovery journal lacks its audit identity")
        staged_audit_binding = raw_audit.get("staged_object")
        if phase in {"prepared", "kill-started", "pane-removed"}:
            reject_unsupported_completed_shell_lifecycle()
        try:
            if phase == "complete":
                if task_state != "replacement" or todo_state != "replacement":
                    raise DrainError("complete recovery journal conflicts with lifecycle source bytes")
                require_clean_lifecycle_exchange(output_directory, root, task, task_data, task_replacement, task_exchange_slot, transition_task)
                require_clean_lifecycle_exchange(output_directory, root, todo, todo_data, todo_replacement, todo_exchange_slot, transition_todo)
                if shell_recovery_state(COMPLETED_SHELL_TARGET, expected_shell, False) != "absent":
                    raise DrainError("complete recovery journal conflicts with a live exact pane")
                if prepared_recovery.staged_audit_fd is None or prepared_recovery.staged_audit_state is None:
                    raise DrainError("complete shell recovery lacks retained staged audit descriptor")
                held_audit = publish_or_validate_staged_audit(
                    output_directory,
                    staged_audit,
                    audit_output.absolute(),
                    audit_data,
                    staged_audit_binding,
                    prepared_recovery.staged_audit_fd,
                    prepared_recovery.staged_audit_state,
                )
                try:
                    return audit
                finally:
                    held_audit.close()
            pane_state = shell_recovery_state(COMPLETED_SHELL_TARGET, expected_shell, False)
            if phase in {"prepared", "kill-started"}:
                if task_state != "source" or todo_state != "source":
                    raise DrainError("lifecycle bytes changed before the durable pane-removed phase")
                if phase == "prepared" and pane_state == "absent":
                    raise DrainError("completed pane is absent without a durable kill-started phase")
                if pane_state == "present":
                    if not postkill_recovery and namespace_authority(root) != authority:
                        raise DrainError("Source-1261 authority drifted before completed-shell removal")
                    if phase == "prepared":
                        phase = transition_shell_recovery(
                            output_directory,
                            journal_path,
                            recovery,
                            phase,
                            "kill-started",
                            audit_data,
                            prepared_recovery.staged_audit_fd,
                            prepared_recovery.staged_audit_state,
                            prepared_recovery.phase_objects.get("kill-started"),
                        )
                    guarded_remove_shell(COMPLETED_SHELL_TARGET, expected_shell)
                    pane_state = shell_recovery_state(COMPLETED_SHELL_TARGET, expected_shell, False)
                if phase == "kill-started" and pane_state == "absent":
                    if not postkill_recovery and namespace_authority(root) != authority:
                        raise ShellRemovalPartialError("completed pane is gone but Source-1261 authority drifted")
                    phase = transition_shell_recovery(
                        output_directory,
                        journal_path,
                        recovery,
                        phase,
                        "pane-removed",
                        audit_data,
                        prepared_recovery.staged_audit_fd,
                        prepared_recovery.staged_audit_state,
                        prepared_recovery.phase_objects.get("pane-removed"),
                    )
            if phase not in {"pane-removed", "files-exchanged"} or pane_state != "absent":
                raise DrainError(f"completed-shell recovery cannot proceed from phase {phase} with pane {pane_state}")
            if phase == "pane-removed":
                if task_state == "source" and todo_state == "replacement":
                    raise DrainError("completed-shell lifecycle bytes have an impossible forward order")
                if task_state == "source":
                    _ = recoverable_replace_task(output_directory, root, task, task_data, task_replacement, task_exchange_slot, transition_task, held_task)
                    task_state = "replacement"
                else:
                    _ = recoverable_replace_task(output_directory, root, task, task_data, task_replacement, task_exchange_slot, transition_task)
                if task_bytes_no_follow(root, task) != task_replacement:
                    raise DrainError("completed-shell task did not reach its exact forward replacement")
                if todo_state == "source":
                    _ = recoverable_replace_task(output_directory, root, todo, todo_data, todo_replacement, todo_exchange_slot, transition_todo, held_todo)
                    todo_state = "replacement"
                else:
                    _ = recoverable_replace_task(output_directory, root, todo, todo_data, todo_replacement, todo_exchange_slot, transition_todo)
                if task_bytes_no_follow(root, todo) != todo_replacement:
                    raise DrainError("completed-shell TODO did not reach its exact forward replacement")
                require_clean_lifecycle_exchange(output_directory, root, task, task_data, task_replacement, task_exchange_slot, transition_task)
                require_clean_lifecycle_exchange(output_directory, root, todo, todo_data, todo_replacement, todo_exchange_slot, transition_todo)
                if not postkill_recovery and namespace_authority(root) != authority:
                    raise ShellRemovalPartialError("completed pane is gone but Source-1261 authority drifted after lifecycle exchange")
                if not postkill_recovery:
                    validate_dirty_snapshot(root, (task, todo), dirty)
                phase = transition_shell_recovery(
                    output_directory,
                    journal_path,
                    recovery,
                    phase,
                    "files-exchanged",
                    audit_data,
                    prepared_recovery.staged_audit_fd,
                    prepared_recovery.staged_audit_state,
                    prepared_recovery.phase_objects.get("files-exchanged"),
                )
            if task_state != "replacement" or todo_state != "replacement":
                raise DrainError("files-exchanged recovery phase lacks exact replacement bytes")
            require_clean_lifecycle_exchange(output_directory, root, task, task_data, task_replacement, task_exchange_slot, transition_task)
            require_clean_lifecycle_exchange(output_directory, root, todo, todo_data, todo_replacement, todo_exchange_slot, transition_todo)
            if shell_recovery_state(COMPLETED_SHELL_TARGET, expected_shell, False) != "absent":
                raise DrainError("completed-shell audit cannot publish while the exact pane remains")
            if not postkill_recovery:
                validate_dirty_snapshot(root, (task, todo), dirty)
            if not postkill_recovery and namespace_authority(root) != authority:
                raise ShellRemovalPartialError("completed pane is gone but Source-1261 authority drifted before audit publication")
            if prepared_recovery.staged_audit_fd is None or prepared_recovery.staged_audit_state is None:
                raise DrainError("completed audit publication lacks retained staged audit descriptor")
            held_audit = publish_or_validate_staged_audit(
                output_directory,
                staged_audit,
                audit_output.absolute(),
                audit_data,
                staged_audit_binding,
                prepared_recovery.staged_audit_fd,
                prepared_recovery.staged_audit_state,
            )
            try:
                revalidate_held_published_audit(output_directory, held_audit, staged_audit.name, audit_output.name)
                _ = transition_shell_recovery(
                    output_directory,
                    journal_path,
                    recovery,
                    phase,
                    "complete",
                    audit_data,
                    held_audit.staged_fd,
                    held_audit.staged_state,
                    prepared_recovery.phase_objects.get("complete"),
                )
                revalidate_held_published_audit(output_directory, held_audit, staged_audit.name, audit_output.name)
                return audit
            finally:
                held_audit.close()
        finally:
            if held_todo is not None:
                held_todo.close()
            if held_task is not None:
                held_task.close()
            prepared_recovery.close()


def close_unslop_done(
    root: Path,
    binding_path: Path,
    binding_sha256: str,
    review_path: Path,
    review_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    audit_output: Path,
    executor: str,
) -> dict[str, object]:
    root = validate_trusted_root(root)
    binding, expected_pane = load_unslop_binding(binding_path, binding_sha256)
    graph_cas_path, graph_cas_sha256, graph_cas = binding_unslop_graph_cas(binding, root)
    review = reconciliation_preflight(root, binding_path, review_path, review_sha256, consumed_receipt, consumed_receipt_sha256, audit_output, executor)
    validate_unslop_output(
        audit_output.resolve(),
        root,
        {binding_path.resolve(), graph_cas_path, review_path.resolve(), consumed_receipt.resolve(), *root.rglob("*.md")},
    )
    executor_pane_id = authenticate_executor(executor)
    validate_binding_route(binding, review_sha256, executor, executor_pane_id)
    task = root / UNSLOP_TASK
    todo = root / "TODO.md"
    task_data, task_replacement = binding_entry(binding, "task", UNSLOP_TASK)
    todo_data, todo_replacement = binding_entry(binding, "todo", "TODO.md")
    raw_shared_manager = binding.get("shared_manager")
    raw_custody_manager = binding.get("custody_manager")
    if not isinstance(raw_shared_manager, dict) or not isinstance(raw_custody_manager, dict):
        raise DrainError("Unslop binding lacks exact manager identities")

    def bound_manager_path(raw: dict[object, object], expected_target: str) -> Path:
        relative = raw.get("task")
        if raw.get("target") != expected_target or not isinstance(relative, str):
            raise DrainError("Unslop binding has an invalid manager target")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path in {task, todo}:
            raise DrainError("Unslop binding has an unsafe manager task path")
        return path

    shared_manager_path = bound_manager_path(raw_shared_manager, UNSLOP_TARGET)
    custody_manager_path = bound_manager_path(raw_custody_manager, UNSLOP_MANAGER)
    raw_authority = binding.get("human_authority")
    if not isinstance(raw_authority, dict):
        raise DrainError("Unslop binding lacks exact Human authority")
    with root_membership_lock(root), task_target_lock(root, UNSLOP_TARGET), ExitStack() as locks:
        graph_paths = lock_complete_task_records(root, locks, {todo})
        if authenticate_executor(executor) != executor_pane_id:
            raise DrainError("executor identity drifted before Unslop reconciliation")
        if task_bytes_no_follow(root, task) != task_data or task_bytes_no_follow(root, todo) != todo_data:
            raise DrainError("Unslop task or TODO bytes drifted")
        derived_replacement = unslop_task_replacement(root, task_data)
        if task_replacement != derived_replacement or todo_replacement != todo_data:
            raise DrainError("Unslop binding does not contain the exact supported replacement")
        graph_before = capture_locked_active_graph(root, graph_paths)
        validate_unslop_graph_cas_pre(root, graph_cas, graph_before, task_data, task_replacement)
        if load_unslop_graph_cas(graph_cas_path, graph_cas_sha256) != graph_cas:
            raise DrainError("Unslop graph/CAS file drifted before reconciliation")
        require_no_unslop_todo_row(root, task, todo_data)
        shared_path, shared_data = active_manager_owner(root, UNSLOP_TARGET)
        custody_path, custody_data = active_manager_owner(root, UNSLOP_MANAGER)
        if (
            shared_path != shared_manager_path
            or raw_shared_manager.get("task_sha256") != sha256_bytes(shared_data)
            or custody_path != custody_manager_path
            or raw_custody_manager.get("task_sha256") != sha256_bytes(custody_data)
        ):
            raise DrainError("Unslop manager identity drifted")
        if set(task_records_for_target(root, UNSLOP_TARGET, active_only=True)) != {task.resolve(), shared_manager_path}:
            raise DrainError("Unslop shared target ownership drifted")
        if shared_pane_snapshot() != expected_pane:
            raise DrainError("Unslop shared pane identity drifted")
        if validate_unslop_authority(root) != raw_authority:
            raise DrainError("Unslop Human authority drifted")
        if unslop_result_snapshot() != binding.get("result"):
            raise DrainError("Unslop completed-and-pushed result identity drifted")
        validate_dirty_snapshot(root, (task,), binding.get("work_log_dirty_manifest"))
        if capture_locked_active_graph(root, graph_paths) != graph_before:
            raise DrainError("complete graph/CAS drifted immediately before Unslop write")
        wrote_task = False
        try:
            atomic_replace_task(root, task, task_data, task_replacement)
            wrote_task = True
            metadata = parse_task_metadata(task_replacement.decode("utf-8"), root)
            if metadata is None or metadata.status != "done" or metadata.blocked_on or metadata.pending_task_items:
                raise DrainError("Unslop replacement is not the exact queue-empty done state")
            if task_bytes_no_follow(root, todo) != todo_data:
                raise DrainError("Unslop TODO changed during reconciliation")
            graph_after = capture_locked_active_graph(root, graph_paths)
            validate_unslop_graph_cas_post(root, graph_cas, graph_after, task_replacement)
            validate_dirty_snapshot(root, (task,), binding.get("work_log_dirty_manifest"))
            if unslop_result_snapshot() != binding.get("result"):
                raise DrainError("Unslop result repository drifted during reconciliation")
            if shared_pane_snapshot() != expected_pane:
                raise DrainError("Unslop shared pane drifted during reconciliation")
            if validate_unslop_authority(root) != raw_authority:
                raise DrainError("Unslop Human authority drifted during reconciliation")
            if load_unslop_graph_cas(graph_cas_path, graph_cas_sha256) != graph_cas:
                raise DrainError("Unslop graph/CAS file drifted during reconciliation")
            if capture_locked_active_graph(root, graph_paths) != graph_after:
                raise DrainError("complete graph/CAS drifted after Unslop validation")
            audit: dict[str, object] = {
                "schema": UNSLOP_AUDIT_SCHEMA,
                "task": UNSLOP_TASK,
                "task_before_sha256": sha256_bytes(task_data),
                "task_after_sha256": sha256_bytes(task_replacement),
                "todo_sha256": sha256_bytes(todo_data),
                "result_commit": UNSLOP_RESULT_COMMIT,
                "result_remote_ref": UNSLOP_RESULT_REF,
                "shared_target": UNSLOP_TARGET,
                "shared_pane_id": expected_pane.pane_id,
                "graph_cas_path": str(graph_cas_path),
                "graph_cas_sha256": graph_cas_sha256,
                "post_projection_sha256": graph_projection_value(graph_after.active_rows)["sha256"],
                "binding_sha256": binding_sha256,
                "code_review_sha256": review_sha256,
                "reviewer": review["reviewer"],
                "executor": executor,
                "email_policy": "suppressed",
                "pending_policy": "unchanged",
                "todo_policy": "unchanged",
                "pane_policy": "untouched",
            }
            write_new_private_output(audit_output.resolve(), canonical_json(audit))
            return audit
        except Exception as error:
            if wrote_task:
                if task_bytes_no_follow(root, task) != task_replacement:
                    raise DrainError("Unslop task changed after write; refusing to overwrite concurrent state during rollback") from error
                try:
                    atomic_replace_task(root, task, task_replacement, task_data)
                    rollback_snapshot = capture_locked_active_graph(root, graph_paths)
                    validate_unslop_graph_cas_rollback(root, graph_before, rollback_snapshot, task_data)
                except Exception as rollback_error:
                    raise DrainError(f"Unslop reconciliation failed and exact rollback validation failed: {rollback_error}") from error
            raise


def namespace_pane_targets(prefixes: tuple[str, ...]) -> tuple[str, ...]:
    raw = tmux_bytes(
        ["list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}"],
        "namespace pane inventory",
    )
    try:
        targets = tuple(sorted({line.strip() for line in raw.decode("utf-8").splitlines() if is_owned(line.strip(), prefixes)}))
    except UnicodeDecodeError as error:
        raise DrainError("namespace pane inventory is not UTF-8") from error
    if any(target.startswith("h") for target in targets):
        raise DrainError("tmux inventory crossed the human-owned namespace boundary")
    return targets


def build_plan(root: Path, prefixes: tuple[str, ...], authority_file: Path, authority_sha256: str, preparer: str = "preparer:1") -> dict[str, object]:
    validate_prefixes(prefixes)
    authority_digest(authority_file, authority_sha256, root)
    tasks = tuple(snapshot for path in sorted(root.rglob("*.md")) if "manager_mail" not in path.parts if (snapshot := task_snapshot(root, path, prefixes)) is not None)
    targets: list[TargetSnapshot] = []
    by_target: dict[str, list[str]] = {}
    for task in tasks:
        try:
            target = canonical_target(task.runat)
        except DrainError as error:
            raise DrainError(f"invalid runat in {task.path}: {task.runat}") from error
        by_target.setdefault(target, []).append(task.path)
    for target in namespace_pane_targets(prefixes):
        by_target.setdefault(target, [])
    for target, paths in sorted(by_target.items()):
        state = inspect_target(target)
        if state == "not_codex":
            raise DrainError(f"not_codex target requires separate reviewed lifecycle reconciliation: {target}")
        pane_id, pane_pid, pane_start_ticks = target_identity(target, state)
        targets.append(TargetSnapshot(target, state, tuple(sorted(paths)), pane_id, pane_pid, pane_start_ticks))
    return {
        "schema": PLAN_SCHEMA,
        "root": str(root.resolve()),
        "root_identity": {"st_dev": root.stat().st_dev, "st_ino": root.stat().st_ino},
        "prefixes": list(prefixes),
        "authority": {"path": str(authority_file.resolve()), "sha256": authority_sha256},
        "preparer": preparer,
        "tasks": [asdict(task) for task in tasks],
        "targets": [asdict(target) for target in targets],
        "email_policy": "suppressed",
    }


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_new_private_output(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def task_bytes_no_follow(root: Path, path: Path) -> bytes:
    relative = path.relative_to(root)
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    if not str(root).startswith("/proc/self/fd/"):
        root_flags |= os.O_NOFOLLOW
    directory_fd = os.open(root, root_flags)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            details = os.fstat(file_fd)
            if not stat.S_ISREG(details.st_mode):
                raise DrainError(f"task is not a no-follow regular file: {path}")
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                return handle.read()
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def lifecycle_parent_descriptor(root: Path, path: Path) -> Iterator[tuple[int, os.stat_result, tuple[os.stat_result, ...]]]:
    relative = path.relative_to(root)
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    if not str(root).startswith("/proc/self/fd/"):
        root_flags |= os.O_NOFOLLOW
    descriptors = [os.open(root, root_flags)]
    states = [os.fstat(descriptors[-1])]
    try:
        for part in relative.parts[:-1]:
            descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=descriptors[-1],
            )
            public = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            state = os.fstat(descriptor)
            if not stat.S_ISDIR(state.st_mode) or directory_stat_identity(public) != directory_stat_identity(state):
                os.close(descriptor)
                raise DrainError("lifecycle parent generation drifted while opening")
            descriptors.append(descriptor)
            states.append(state)
        yield descriptors[-1], states[-1], tuple(states)
        for descriptor, initial in zip(descriptors, states, strict=True):
            if directory_stat_identity(os.fstat(descriptor)) != directory_stat_identity(initial):
                raise DrainError("lifecycle parent generation drifted during exchange")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def lifecycle_parent_chain_value(root: Path, path: Path, states: tuple[os.stat_result, ...]) -> tuple[dict[str, object], ...]:
    names = ("", *path.relative_to(root).parts[:-1])
    if len(names) != len(states):
        raise DrainError("lifecycle parent chain binding is incomplete")
    return tuple(
        {
            "index": index,
            "name": names[index],
            "device": state.st_dev,
            "inode": state.st_ino,
            "mode": state.st_mode,
            "uid": state.st_uid,
        }
        for index, state in enumerate(states)
    )


def lifecycle_parent_identity(root: Path, path: Path) -> dict[str, int]:
    with lifecycle_parent_descriptor(root, path) as (_, details, _):
        return {
            "device": details.st_dev,
            "inode": details.st_ino,
            "uid": details.st_uid,
            "mode": stat.S_IMODE(details.st_mode),
        }


def current_lifecycle_parent_chain(root: Path, path: Path) -> tuple[dict[str, object], ...]:
    with lifecycle_parent_descriptor(root, path) as (_, _, chain_states):
        return lifecycle_parent_chain_value(root, path, chain_states)


@dataclass
class HeldLifecycleExchange:
    directory_fd: int
    public_fd: int
    private_fd: int
    directory_state: os.stat_result
    public_data: bytes
    public_state: os.stat_result
    private_data: bytes
    private_state: os.stat_result

    def close(self) -> None:
        os.close(self.private_fd)
        os.close(self.public_fd)
        os.close(self.directory_fd)


def lifecycle_entry_bytes(
    directory_fd: int,
    name: str,
    label: str,
) -> tuple[int, bytes, os.stat_result] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    try:
        data, details = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, label)
        public = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stable_stat_identity(public) != stable_stat_identity(details):
            raise DrainError(f"{label} public identity drifted")
        return descriptor, data, details
    except Exception:
        os.close(descriptor)
        raise


def revalidate_lifecycle_entry(
    directory_fd: int,
    name: str,
    descriptor: int,
    expected_data: bytes,
    expected_state: os.stat_result,
    label: str,
) -> None:
    data, held = read_bounded_fd(descriptor, MAX_PRIVATE_ARTIFACT_BYTES, label)
    public = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        data != expected_data
        or stable_stat_identity(held) != stable_stat_identity(expected_state)
        or stable_stat_identity(public) != stable_stat_identity(expected_state)
    ):
        raise DrainError(f"{label} identity or bytes drifted")


def absent_lifecycle_entry_identity(directory_fd: int, name: str, label: str) -> dict[str, object]:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return {"name": name, "absent": True}
    raise DrainError(f"{label} still exists")


def write_lifecycle_exchange_slot(directory_fd: int, name: str, data: bytes, mode: int) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise DrainError("lifecycle exchange-slot write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        created = os.fstat(descriptor)
        public = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stable_stat_identity(public) != stable_stat_identity(created):
            raise DrainError("lifecycle exchange slot was replaced during creation")
    finally:
        os.close(descriptor)
    os.fsync(directory_fd)


def prestage_lifecycle_exchange_slot(
    output_directory: PinnedDirectory,
    root: Path,
    path: Path,
    source: bytes,
    replacement: bytes,
    slot_name: str,
) -> dict[str, object]:
    if "/" in slot_name or not slot_name:
        raise DrainError("lifecycle exchange slot name is invalid")
    with lifecycle_parent_descriptor(root, path) as (directory_fd, directory_state, chain_states):
        if output_directory.states[-1].st_dev != directory_state.st_dev:
            raise DrainError("lifecycle exchange private directory is on a different device")
        public_entry = lifecycle_entry_bytes(directory_fd, path.name, "lifecycle public entry")
        if public_entry is None:
            raise DrainError("lifecycle public entry disappeared while staging exchange")
        public_fd, public_data, public_state = public_entry
        private_fd = -1
        try:
            if public_data != source:
                raise DrainError("lifecycle public entry drifted before staging exchange")
            revalidate_lifecycle_entry(directory_fd, path.name, public_fd, source, public_state, "source lifecycle entry")
            private_entry = pinned_held_object(
                output_directory,
                slot_name,
                stat.S_IMODE(public_state.st_mode),
                "private lifecycle replacement",
            )
            if private_entry is None:
                pinned_write_new_object(output_directory, slot_name, replacement, stat.S_IMODE(public_state.st_mode))
                private_entry = pinned_held_object(output_directory, slot_name, stat.S_IMODE(public_state.st_mode), "private lifecycle replacement")
                if private_entry is None:
                    raise DrainError("private lifecycle replacement disappeared after staging")
            private_fd, private_data, private_state = private_entry
            if private_data != replacement:
                raise DrainError("private lifecycle replacement contains foreign bytes")
            verified_fd, _ = validate_pinned_object_binding(
                output_directory,
                pinned_object_binding(slot_name, replacement, private_state),
                replacement,
                stat.S_IMODE(public_state.st_mode),
                "private lifecycle replacement",
            )
            os.close(verified_fd)
            return {
                "path": str(path.relative_to(root)),
                "exchange_slot": slot_name,
                "lifecycle_directory": exchange_object_value(directory_state),
                "lifecycle_directory_chain": list(lifecycle_parent_chain_value(root, path, chain_states)),
                "private_directory": exchange_object_value(output_directory.states[-1]),
                "source_object": exchange_object_value(public_state),
                "source_sha256": sha256_bytes(source),
                "replacement_object": exchange_object_value(private_state),
                "replacement_sha256": sha256_bytes(replacement),
            }
        finally:
            if private_fd >= 0:
                os.close(private_fd)
            os.close(public_fd)


def validate_lifecycle_binding(raw: object, root: Path, path: Path, source: bytes, replacement: bytes, slot_name: str) -> dict[str, object]:
    relative = str(path.relative_to(root))
    if (
        not isinstance(raw, dict)
        or raw.get("path") != relative
        or raw.get("exchange_slot") != slot_name
        or raw.get("source_sha256") != sha256_bytes(source)
        or raw.get("replacement_sha256") != sha256_bytes(replacement)
        or not isinstance(raw.get("source_object"), dict)
        or not isinstance(raw.get("replacement_object"), dict)
        or not isinstance(raw.get("lifecycle_directory"), dict)
        or not isinstance(raw.get("lifecycle_directory_chain"), list)
        or not isinstance(raw.get("private_directory"), dict)
    ):
        raise DrainError("lifecycle exchange binding is invalid")
    if raw["lifecycle_directory_chain"] != list(current_lifecycle_parent_chain(root, path)):
        raise DrainError("lifecycle exchange directory chain drifted")
    return raw


def open_held_lifecycle_exchange(
    output_directory: PinnedDirectory,
    root: Path,
    path: Path,
    source: bytes,
    replacement: bytes,
    slot_name: str,
    binding: dict[str, object],
) -> HeldLifecycleExchange:
    relative = path.relative_to(root)
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    if not str(root).startswith("/proc/self/fd/"):
        root_flags |= os.O_NOFOLLOW
    descriptors = [os.open(root, root_flags)]
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=descriptors[-1])
            descriptors.append(next_fd)
        directory_fd = descriptors.pop()
    except Exception:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise
    for descriptor in reversed(descriptors):
        os.close(descriptor)
    public_entry = lifecycle_entry_bytes(directory_fd, path.name, "held lifecycle public entry")
    private_entry = pinned_held_object(output_directory, slot_name, int(binding["replacement_object"]["mode"]) & 0o777, "held private lifecycle replacement")
    if public_entry is None or private_entry is None:
        os.close(directory_fd)
        if public_entry is not None:
            os.close(public_entry[0])
        raise DrainError("held lifecycle exchange objects are incomplete")
    public_fd, public_data, public_state = public_entry
    private_fd, private_data, private_state = private_entry
    try:
        if (
            public_data != source
            or private_data != replacement
            or not exchange_object_matches(public_state, binding["source_object"])
            or not exchange_object_matches(private_state, binding["replacement_object"])
        ):
            raise DrainError("held lifecycle exchange identity drifted")
        return HeldLifecycleExchange(directory_fd, public_fd, private_fd, os.fstat(directory_fd), public_data, public_state, private_data, private_state)
    except Exception:
        os.close(private_fd)
        os.close(public_fd)
        os.close(directory_fd)
        raise


def recoverable_replace_task(
    output_directory: PinnedDirectory,
    root: Path,
    path: Path,
    source: bytes,
    replacement: bytes,
    slot_name: str,
    binding: dict[str, object],
    held: HeldLifecycleExchange | None = None,
) -> dict[str, object]:
    """Complete or recover one deterministic post-kill lifecycle exchange."""
    raise DrainError("lifecycle RENAME_EXCHANGE cannot prove descriptor-bound task/TODO mutation against same-UID name swaps")
    if "/" in slot_name or not slot_name:
        raise DrainError("lifecycle exchange slot name is invalid")
    with lifecycle_parent_descriptor(root, path) as (directory_fd, directory_state, _):
        if output_directory.states[-1].st_dev != directory_state.st_dev:
            raise DrainError("lifecycle exchange private directory is on a different device")
        binding = validate_lifecycle_binding(binding, root, path, source, replacement, slot_name)
        if not exchange_object_matches(directory_state, binding["lifecycle_directory"]) or not exchange_object_matches(output_directory.states[-1], binding["private_directory"]):
            raise DrainError("lifecycle exchange directory identity drifted")
        if held is None:
            public_entry = lifecycle_entry_bytes(directory_fd, path.name, "lifecycle public entry")
            if public_entry is None:
                raise DrainError("lifecycle public entry disappeared during recovery")
            public_fd, public_data, public_state = public_entry
            private_entry = pinned_held_object(
                output_directory,
                slot_name,
                stat.S_IMODE(public_state.st_mode),
                "private lifecycle exchange object",
            )
            private_fd = -1
        else:
            if directory_stat_identity(held.directory_state) != directory_stat_identity(directory_state):
                raise DrainError("held lifecycle exchange directory drifted")
            public_fd, public_data, public_state = held.public_fd, held.public_data, held.public_state
            private_fd, private_data, private_state = held.private_fd, held.private_data, held.private_state
            private_entry = (private_fd, private_data, private_state)
        try:
            if private_entry is None:
                raise DrainError("private lifecycle exchange object is absent")
            if held is None:
                private_fd, private_data, private_state = private_entry

            if public_data == replacement and private_data == source:
                revalidate_lifecycle_entry(directory_fd, path.name, public_fd, replacement, public_state, "replacement lifecycle entry")
                if not exchange_object_matches(public_state, binding["replacement_object"]) or not exchange_object_matches(private_state, binding["source_object"]):
                    raise DrainError("lifecycle post-exchange object identity drifted")
                retained_fd, _ = validate_pinned_bound_object(
                    output_directory,
                    slot_name,
                    source,
                    private_state,
                    stat.S_IMODE(private_state.st_mode),
                    "private retained lifecycle source",
                )
                os.close(retained_fd)
                os.fsync(directory_fd)
                os.fsync(output_directory.descriptor)
                return {
                    "path": str(path.relative_to(root)),
                    "exchange_slot": slot_name,
                    "lifecycle_directory": exchange_object_value(directory_state),
                    "private_directory": exchange_object_value(output_directory.states[-1]),
                    "public_object": exchange_object_value(public_state),
                    "public_sha256": sha256_bytes(replacement),
                    "retained_source_object": exchange_object_value(private_state),
                    "retained_source_sha256": sha256_bytes(source),
                }

            if public_data != source or private_data != replacement:
                raise DrainError("lifecycle exchange state matches neither exact pre-state nor exact post-state")
            if not exchange_object_matches(public_state, binding["source_object"]) or not exchange_object_matches(private_state, binding["replacement_object"]):
                raise DrainError("lifecycle pre-exchange object identity drifted")
            revalidate_lifecycle_entry(directory_fd, path.name, public_fd, source, public_state, "source lifecycle entry")
            verified_fd, _ = validate_pinned_object_binding(
                output_directory,
                pinned_object_binding(slot_name, replacement, private_state),
                replacement,
                stat.S_IMODE(private_state.st_mode),
                "private lifecycle replacement",
            )
            os.close(verified_fd)
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int
            if renameat2(output_directory.descriptor, slot_name.encode(), directory_fd, path.name.encode(), RENAME_EXCHANGE) != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number), str(path))
            os.fsync(directory_fd)
            os.fsync(output_directory.descriptor)
            public_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            private_after = os.stat(slot_name, dir_fd=output_directory.descriptor, follow_symlinks=False)
            if (
                exchange_object_identity(public_after) != exchange_object_identity(private_state)
                or exchange_object_identity(private_after) != exchange_object_identity(public_state)
            ):
                raise DrainError("lifecycle exchange installed unexpected entry identities; both names retained")
            revalidate_lifecycle_exchanged_entry(directory_fd, path.name, private_fd, replacement, private_state, "installed lifecycle replacement")
            retained_fd, _ = validate_pinned_bound_object(
                output_directory,
                slot_name,
                source,
                public_state,
                stat.S_IMODE(public_state.st_mode),
                "private retained lifecycle source",
            )
            os.close(retained_fd)
            return {
                "path": str(path.relative_to(root)),
                "exchange_slot": slot_name,
                "lifecycle_directory": exchange_object_value(directory_state),
                "private_directory": exchange_object_value(output_directory.states[-1]),
                "public_object": exchange_object_value(private_state),
                "public_sha256": sha256_bytes(replacement),
                "retained_source_object": exchange_object_value(public_state),
                "retained_source_sha256": sha256_bytes(source),
            }
        finally:
            if held is None and private_fd >= 0:
                os.close(private_fd)
            if held is None:
                os.close(public_fd)


def require_clean_lifecycle_exchange(
    output_directory: PinnedDirectory,
    root: Path,
    path: Path,
    source: bytes,
    replacement: bytes,
    slot_name: str,
    binding: dict[str, object],
) -> None:
    with lifecycle_parent_descriptor(root, path) as (directory_fd, _, _):
        binding = validate_lifecycle_binding(binding, root, path, source, replacement, slot_name)
        public_entry = lifecycle_entry_bytes(directory_fd, path.name, "completed lifecycle public entry")
        if public_entry is None:
            raise DrainError("completed lifecycle public entry is absent")
        descriptor, data, public_state = public_entry
        try:
            if data != replacement or not exchange_object_matches(public_state, binding["replacement_object"]):
                raise DrainError("completed lifecycle public entry lacks its exact replacement")
        finally:
            os.close(descriptor)
        private_entry = pinned_held_object(
            output_directory,
            slot_name,
            int(binding["source_object"]["mode"]) & 0o777,
            "completed lifecycle retained source",
        )
        if private_entry is None:
            raise DrainError("completed lifecycle retained source is absent")
        private_fd, private_data, private_state = private_entry
        try:
            if private_data != source or not exchange_object_matches(private_state, binding["source_object"]):
                raise DrainError("completed lifecycle retained source drifted")
        finally:
            os.close(private_fd)
        os.fsync(directory_fd)
        os.fsync(output_directory.descriptor)


def atomic_replace_task(root: Path, path: Path, expected: bytes, replacement: bytes) -> None:
    relative = path.relative_to(root)
    root_flags = os.O_RDONLY | os.O_DIRECTORY
    if not str(root).startswith("/proc/self/fd/"):
        root_flags |= os.O_NOFOLLOW
    directory_fd = os.open(root, root_flags)
    temporary_name = f".{relative.name}.namespace-drain-{os.getpid()}-{os.urandom(8).hex()}"
    retain_temporary = False
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            details = os.fstat(file_fd)
            if not stat.S_ISREG(details.st_mode):
                raise DrainError(f"task is not a no-follow regular file: {path}")
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                current = handle.read()
        finally:
            os.close(file_fd)
        if current != expected:
            raise DrainError(f"task drift immediately before directory-bound write: {path}")
        original_mode = stat.S_IMODE(details.st_mode)
        temporary_fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, original_mode, dir_fd=directory_fd)
        try:
            os.fchmod(temporary_fd, original_mode)
            with os.fdopen(temporary_fd, "wb", closefd=False) as handle:
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(temporary_fd)
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(directory_fd, temporary_name.encode(), directory_fd, relative.name.encode(), RENAME_EXCHANGE) != 0:
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), str(path))
        exchanged = os.stat(temporary_name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(exchanged.st_mode) or (exchanged.st_dev, exchanged.st_ino) != (details.st_dev, details.st_ino):
            retain_temporary = True
            raise TaskExchangeError(path, temporary_name, f"task entry identity changed after atomic exchange; both names preserved without unsafe rollback: {path}")
        os.unlink(temporary_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if not retain_temporary:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def validate_new_private_output(path: Path, root: Path, protected: set[Path]) -> None:
    path = path.resolve(strict=False)
    root = root.resolve()
    if path == root or path.is_relative_to(root) or path in protected or path.exists() or path.is_symlink():
        raise DrainError(f"output must be a new disjoint file outside the work-log root: {path}")
    parent = path.parent
    details = parent.stat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise DrainError(f"output parent must be an owner-private directory: {parent}")


def validate_review(
    path: Path,
    expected_sha256: str,
    plan_sha256: str,
    reviewer_task: Path,
    reviewer_task_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    root: Path,
    preparer: str,
    executor: str,
    prefixes: tuple[str, ...],
) -> None:
    for role, actor in (("preparer", preparer), ("executor", executor)):
        if TARGET_RE.fullmatch(actor) is None or actor.startswith("h"):
            raise DrainError(f"{role} must be an exact non-Human target")
    data = path.read_bytes()
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
        raise DrainError("review receipt is not an owner-private no-follow regular file")
    if sha256_bytes(data) != expected_sha256:
        raise DrainError("review receipt digest mismatch")
    review = json.loads(data)
    if not isinstance(review, dict) or review.get("schema") != REVIEW_SCHEMA:
        raise DrainError("invalid review receipt schema")
    if review.get("plan_sha256") != plan_sha256 or review.get("verdict") != "PASS":
        raise DrainError("review receipt is not an exact PASS for this plan")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"]:
        raise DrainError("review receipt has no independent reviewer identity")
    report_agent = review.get("report_agent")
    if not isinstance(report_agent, str) or REPORT_AGENT_RE.fullmatch(report_agent) is None or report_agent in {".", ".."}:
        raise DrainError("review receipt has no valid report-agent identity")
    reviewer_data = reviewer_task.read_bytes()
    reviewer_resolved = reviewer_task.resolve()
    reviewer_details = reviewer_task.lstat()
    if (
        not reviewer_resolved.is_relative_to(root)
        or stat.S_ISLNK(reviewer_details.st_mode)
        or not stat.S_ISREG(reviewer_details.st_mode)
        or reviewer_details.st_uid != os.getuid()
        or stat.S_IMODE(reviewer_details.st_mode) & 0o022
    ):
        raise DrainError("reviewer task is not a trusted no-follow owner-controlled work-log record")
    if sha256_bytes(reviewer_data) != reviewer_task_sha256 or review.get("reviewer_task_sha256") != reviewer_task_sha256:
        raise DrainError("reviewer task identity mismatch")
    reviewer_metadata = parse_task_metadata(reviewer_data.decode("utf-8"), work_log_root=reviewer_task.parent)
    if reviewer_metadata is None or reviewer_metadata.status not in {"running", "long_running", "blocked"} or reviewer_metadata.runat != review["reviewer"]:
        raise DrainError("reviewer receipt is not bound to its task owner")
    if review["reviewer"] in {preparer, executor} or is_owned(str(review["reviewer"]), prefixes):
        raise DrainError("reviewer is not independent of preparer, executor, and drained namespaces")
    if str(review["reviewer"]).startswith("h"):
        raise DrainError("reviewer must not be a Human-owned target")
    consumed_data = consumed_receipt.read_bytes()
    consumed_details = consumed_receipt.lstat()
    if stat.S_ISLNK(consumed_details.st_mode) or not stat.S_ISREG(consumed_details.st_mode) or consumed_details.st_uid != os.getuid() or stat.S_IMODE(consumed_details.st_mode) != 0o600:
        raise DrainError("consumed report receipt is not an owner-private no-follow regular file")
    if sha256_bytes(consumed_data) != consumed_receipt_sha256:
        raise DrainError("consumed independent-report receipt digest mismatch")
    if consumed_receipt.resolve().parent != TRUSTED_RECEIPT_DIR.resolve():
        raise DrainError("consumed review receipt is outside the trusted report-receipt store")
    consumed = json.loads(consumed_data)
    consumed_input = consumed.get("input") if isinstance(consumed, dict) else None
    attestation = consumed.get("attestation_id") if isinstance(consumed, dict) else None
    unsigned = {key: value for key, value in consumed.items() if key != "attestation_id"} if isinstance(consumed, dict) else {}
    required_closure = {
        "accepted",
        "attestation_id",
        "consumed_at_unix_s",
        "input",
        "reason",
        "recovery_residue",
        "replay_id",
        "schema",
        "status",
        "terminal",
        "transfer_receipt",
    }
    if (
        not isinstance(consumed, dict)
        or set(consumed) != required_closure
        or consumed.get("schema") != "omo-report-consumed-closure/v1"
        or consumed.get("accepted") is not False
        or consumed.get("terminal") is not True
        or not isinstance(consumed_input, dict)
        or consumed_input.get("file_sha256") != expected_sha256
        or attestation != bound_receipt_id(unsigned)
    ):
        raise DrainError("independent review lacks exact consumed-report provenance")
    verify_consumed_report(path, str(review["reviewer"]), report_agent, str(consumed.get("status", "")), consumed_data, root)


def verify_consumed_report(review_path: Path, reviewer: str, report_agent: str, status: str, expected_attestation: bytes, root: Path) -> None:
    pane_id = exact_pane_id(reviewer)
    if not pane_id:
        raise DrainError("reviewer pane is unavailable for route-bound consumed verification")
    result = subprocess.run(
        [str(Path(__file__).with_name("omo_report.sh")), "--verify-consumed", "--status", status, "--message-file", str(review_path), "--agent", report_agent],
        capture_output=True,
        timeout=60,
        check=False,
        env={**os.environ, "TMUX_PANE": pane_id, "OMO_WORK_LOGS_ROOT": str(root), "OMO_AGENT_NAME": report_agent},
    )
    if result.returncode != 0 or result.stdout != expected_attestation:
        raise DrainError("supported report verifier did not authenticate the consumed independent review")


def rewritten_task(data: bytes, ledger_path: Path, ledger_sha256: str) -> bytes:
    text = data.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise DrainError("task lost frontmatter during drain")
    end = lines[1:].index("---") + 1
    values = yaml.load("\n".join(lines[1:end]), Loader=UniqueKeyLoader)
    if not isinstance(values, dict):
        raise DrainError("task frontmatter is invalid")
    version = values.get("version")
    original_status = values.get("status")
    values["status"] = "blocked"
    values["runat"] = "retired"
    if version == "v2.0.0":
        if original_status in {"running", "long_running"}:
            values["resume_status"] = original_status
        blockers = values.get("blocked_on", [])
        if not isinstance(blockers, list):
            raise DrainError("v2 blocked_on is not a structured list")
        marker = {"kind": "persistent", "reason": f"namespace drain preserved in {ledger_path} sha256={ledger_sha256}"}
        values["blocked_on"] = [blocker for blocker in blockers if not isinstance(blocker, dict) or blocker.get("kind") != "pending_items"]
        values["blocked_on"] = [*values["blocked_on"], *([] if marker in values["blocked_on"] else [marker])]
        values["pending_task_items"] = []
    else:
        values["pending_task_items"] = []
        values["blocked_on"] = f"namespace drain preserved in {ledger_path} sha256={ledger_sha256}"
    frontmatter = yaml.safe_dump(values, sort_keys=False).rstrip()
    note = f"\n(namespace drain: preserved in {ledger_path} sha256={ledger_sha256})\n"
    return f"---\n{frontmatter}\n---\n".encode() + "\n".join(lines[end + 1 :]).encode() + note.encode()


def stop_target(target: str, pane_id: str, pane_pid: int, pane_start_ticks: int, session_id: str = "") -> None:
    current_state = inspect_target(target)
    current_identity = target_identity(target, current_state)
    if current_state not in STOPPABLE or current_identity != (pane_id, pane_pid, pane_start_ticks):
        raise DrainError(f"guarded stop identity drift for {target}")
    _ = guarded_codex_stop(
        CodexStopArgs(
            target=target,
            wait_s=10.0,
            lines=2000,
            dry_run=False,
            allow_self=False,
            no_feedback=True,
            bound_symbolic_target=target,
            bound_pane_id=pane_id,
            bound_pane_pid=pane_pid,
            bound_pane_start_ticks=pane_start_ticks,
            bound_expected_session_id=session_id,
        )
    )


def _execute_unlocked(
    plan_path: Path,
    expected_plan_sha256: str,
    review_path: Path,
    expected_review_sha256: str,
    ledger_path: Path,
    reviewer_task: Path,
    reviewer_task_sha256: str,
    consumed_receipt: Path,
    consumed_receipt_sha256: str,
    executor: str,
    anchored_root: Path,
) -> dict[str, object]:
    plan_data = plan_path.read_bytes()
    if sha256_bytes(plan_data) != expected_plan_sha256:
        raise DrainError("plan digest mismatch")
    plan = read_json(plan_path)
    if plan.get("schema") != PLAN_SCHEMA or plan.get("email_policy") != "suppressed":
        raise DrainError("invalid or mail-enabled plan")
    root = anchored_root
    authority = plan.get("authority")
    if not isinstance(authority, dict):
        raise DrainError("plan authority is missing")
    authority_digest(Path(str(authority["path"])), str(authority["sha256"]), root)
    raw_prefixes = plan.get("prefixes")
    if not isinstance(raw_prefixes, list) or not all(isinstance(prefix, str) for prefix in raw_prefixes):
        raise DrainError("plan prefixes are invalid")
    prefixes = tuple(raw_prefixes)
    preparer = plan.get("preparer")
    if not isinstance(preparer, str) or not preparer or executor == preparer:
        raise DrainError("preparer and executor identities must be distinct")
    validate_review(
        review_path,
        expected_review_sha256,
        expected_plan_sha256,
        reviewer_task,
        reviewer_task_sha256,
        consumed_receipt,
        consumed_receipt_sha256,
        Path(str(plan["root"])).resolve(),
        preparer,
        executor,
        prefixes,
    )
    fresh_plan = build_plan(root, prefixes, Path(str(authority["path"])), str(authority["sha256"]), preparer)
    if canonical_json(fresh_plan) != plan_data:
        raise DrainError("complete task or pane inventory drift before drain")
    raw_tasks = plan.get("tasks")
    raw_targets = plan.get("targets")
    if not isinstance(raw_tasks, list) or not isinstance(raw_targets, list):
        raise DrainError("plan inventory is invalid")
    task_inputs: list[tuple[Path, bytes]] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise DrainError("invalid task inventory entry")
        path = root / str(raw["path"])
        data = path.read_bytes()
        if sha256_bytes(data) != raw.get("sha256"):
            raise DrainError(f"task drift before drain: {path}")
        encoded = raw.get("source_base64")
        if not isinstance(encoded, str) or base64.b64decode(encoded, validate=True) != data:
            raise DrainError(f"complete task preservation mismatch: {path}")
        task_inputs.append((path, data))
    current_states: dict[str, tuple[str, str, int, int]] = {}
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise DrainError("invalid target inventory entry")
        target = str(raw["target"])
        state = inspect_target(target)
        if state != raw.get("state"):
            raise DrainError(f"target drift before drain: {target}")
        identity = target_identity(target, state)
        expected_identity = (str(raw.get("pane_id", "")), int(raw.get("pane_pid", 0)), int(raw.get("pane_start_ticks", 0)))
        if identity != expected_identity:
            raise DrainError(f"target identity drift before drain: {target}")
        current_states[target] = (state, *identity)
    ledger: dict[str, object] = {
        "schema": LEDGER_SCHEMA,
        "plan_sha256": expected_plan_sha256,
        "review_sha256": expected_review_sha256,
        "authority": authority,
        "created_at": datetime.now(UTC).isoformat(),
        "tasks": raw_tasks,
        "targets": raw_targets,
        "email_policy": "suppressed",
    }
    ledger_data = canonical_json(ledger)
    ledger_sha256 = sha256_bytes(ledger_data)
    atomic_write(ledger_path, ledger_data)
    progress_path = ledger_path.with_name(f"{ledger_path.name}.progress.json")
    stopped: list[str] = []
    written: list[tuple[Path, bytes, bytes]] = []
    try:
        for target, (state, pane_id, pane_pid, pane_start_ticks) in current_states.items():
            if state in STOPPABLE:
                progress: dict[str, object] = {"schema": "omo-namespace-drain-progress/v1", "ledger_sha256": ledger_sha256, "phase": "before-stop", "target": target, "stopped_targets": stopped}
                atomic_write(progress_path, canonical_json(progress))
                stop_target(target, pane_id, pane_pid, pane_start_ticks)
                stopped.append(target)
                progress["phase"] = "after-stop"
                progress["stopped_targets"] = stopped
                atomic_write(progress_path, canonical_json(progress))
        for path, data in task_inputs:
            if task_bytes_no_follow(root, path) != data:
                raise DrainError(f"task drift immediately before write: {path}")
            atomic_write(
                progress_path,
                canonical_json({"schema": "omo-namespace-drain-progress/v1", "ledger_sha256": ledger_sha256, "phase": "before-task-write", "task": str(path), "stopped_targets": stopped}),
            )
            replacement = rewritten_task(data, ledger_path, ledger_sha256)
            atomic_replace_task(root, path, data, replacement)
            written.append((path, data, replacement))
    except Exception as error:
        restored: list[str] = []
        rollback_blocked: list[str] = []
        partial_exchange_evidence: list[dict[str, str]] = []
        if isinstance(error, TaskExchangeError):
            rollback_blocked.append(str(error.path))
            partial_exchange_evidence.append({"task": str(error.path), "retained_exchange_name": error.temporary_name})
        for path, original, replacement in reversed(written):
            try:
                if task_bytes_no_follow(root, path) == replacement:
                    atomic_replace_task(root, path, replacement, original)
                    restored.append(str(path))
                else:
                    rollback_blocked.append(str(path))
            except Exception as rollback_error:
                rollback_blocked.append(str(path))
                if isinstance(rollback_error, TaskExchangeError):
                    partial_exchange_evidence.append({"task": str(rollback_error.path), "retained_exchange_name": rollback_error.temporary_name})
        recovery: dict[str, object] = {
            "schema": "omo-namespace-drain-progress/v1",
            "ledger_sha256": ledger_sha256,
            "partial_failure": str(error),
            "stopped_targets": stopped,
            "restored_tasks": restored,
            "rollback_blocked_tasks": sorted(set(rollback_blocked)),
            "partial_exchange_evidence": partial_exchange_evidence,
        }
        atomic_write(progress_path, canonical_json(recovery))
        raise
    try:
        remaining = [target for target in current_states if inspect_target(target) not in PASSIVE]
        active_runat = []
        for path, _ in task_inputs:
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), work_log_root=root)
            if metadata is None or metadata.runat != "retired":
                active_runat.append(str(path))
        if remaining or active_runat:
            raise DrainError(f"final verification failed: live={remaining} active_runat={active_runat}")
        namespace_live_rows: list[str] = []
        for target in namespace_pane_targets(prefixes):
            state = inspect_target(target)
            original = current_states.get(target)
            if original is None or state not in PASSIVE or target_identity(target, state) != original[1:]:
                namespace_live_rows.append(target)
        namespace_live = tuple(namespace_live_rows)
        if namespace_live:
            raise DrainError(f"final namespace pane verification failed: {list(namespace_live)}")
        newly_active = [str(path.relative_to(root)) for path in sorted(root.rglob("*.md")) if "manager_mail" not in path.parts and task_snapshot(root, path, prefixes) is not None]
        if newly_active:
            raise DrainError(f"final namespace task verification failed: {newly_active}")
    except Exception as error:
        atomic_write(
            progress_path,
            canonical_json({"schema": "omo-namespace-drain-progress/v1", "ledger_sha256": ledger_sha256, "partial_failure": str(error), "stopped_targets": stopped, "phase": "final-verification"}),
        )
        raise
    receipt: dict[str, object] = {
        "schema": "omo-namespace-drain-progress/v1",
        "ledger_sha256": ledger_sha256,
        "completed_at": datetime.now(UTC).isoformat(),
        "task_count": len(task_inputs),
        "stopped_targets": stopped,
        "final_live_targets": [],
        "final_active_runat": [],
    }
    atomic_write(progress_path, canonical_json(receipt))
    return receipt


def execute(
    plan_path: Path,
    expected_plan_sha256: str,
    review_path: Path,
    expected_review_sha256: str,
    ledger_path: Path,
    reviewer_task: Path | None = None,
    reviewer_task_sha256: str = "",
    consumed_receipt: Path | None = None,
    consumed_receipt_sha256: str = "",
    executor: str = "executor:1",
) -> dict[str, object]:
    plan = read_json(plan_path)
    root_value = plan.get("root")
    if not isinstance(root_value, str) or not Path(root_value).is_absolute():
        raise DrainError("plan root is invalid")
    lock_path = Path(root_value) / ".omo-namespace-drain.lock"
    authority = plan.get("authority")
    tasks = plan.get("tasks")
    protected = {plan_path.resolve(), review_path.resolve(), reviewer_task.resolve() if reviewer_task is not None else review_path.with_name("reviewer.md").resolve()}
    if isinstance(authority, dict) and isinstance(authority.get("path"), str):
        protected.add(Path(str(authority["path"])).resolve())
    if isinstance(tasks, list):
        protected.update(Path(root_value, str(task["path"])).resolve() for task in tasks if isinstance(task, dict) and isinstance(task.get("path"), str))
    validate_new_private_output(ledger_path, Path(root_value), protected)
    validate_new_private_output(ledger_path.with_name(f"{ledger_path.name}.progress.json"), Path(root_value), protected | {ledger_path.resolve()})
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise DrainError("another namespace drain holds the serialized transaction lock") from error
        root_fd = os.open(root_value, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            details = os.fstat(root_fd)
            identity = plan.get("root_identity")
            if not isinstance(identity, dict) or identity != {"st_dev": details.st_dev, "st_ino": details.st_ino}:
                raise DrainError("work-log root identity drift before drain")
            if reviewer_task is None:
                reviewer_task = review_path.with_name("reviewer.md")
            if consumed_receipt is None:
                consumed_receipt = review_path.with_name("consumed.json")
            return _execute_unlocked(
                plan_path,
                expected_plan_sha256,
                review_path,
                expected_review_sha256,
                ledger_path,
                reviewer_task,
                reviewer_task_sha256,
                consumed_receipt,
                consumed_receipt_sha256,
                executor,
                Path(f"/proc/self/fd/{root_fd}"),
            )
        finally:
            os.close(root_fd)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--root", type=Path, required=True)
    plan.add_argument("--prefix", action="append", required=True)
    plan.add_argument("--authority-file", type=Path, required=True)
    plan.add_argument("--authority-sha256", required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--preparer", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--plan", type=Path, required=True)
    apply.add_argument("--plan-sha256", required=True)
    apply.add_argument("--review", type=Path, required=True)
    apply.add_argument("--review-sha256", required=True)
    apply.add_argument("--ledger", type=Path, required=True)
    apply.add_argument("--reviewer-task", type=Path, required=True)
    apply.add_argument("--reviewer-task-sha256", required=True)
    apply.add_argument("--executor", required=True)
    apply.add_argument("--consumed-review-receipt", type=Path, required=True)
    apply.add_argument("--consumed-review-receipt-sha256", required=True)
    for name in (
        "bind-taskless-shell",
        "reconcile-taskless-shell",
        "bind-completed-shell",
        "reconcile-completed-shell",
        "bind-unslop-done",
        "reconcile-unslop-done",
        "bind-absent-manager-history",
        "reconcile-absent-manager-history",
        "bind-source1385-live-worker-handoff",
        "reconcile-source1385-live-worker-handoff",
    ):
        narrow = subparsers.add_parser(name)
        narrow.add_argument("--root", type=Path, required=True)
        narrow.add_argument("--code-review", type=Path, required=True)
        narrow.add_argument("--code-review-sha256", required=True)
        narrow.add_argument("--consumed-code-review-receipt", type=Path, required=True)
        narrow.add_argument("--consumed-code-review-receipt-sha256", required=True)
        narrow.add_argument("--executor", required=True)
        if name.startswith("bind-"):
            narrow.add_argument("--output", type=Path, required=True)
        else:
            narrow.add_argument("--binding", type=Path, required=True)
            narrow.add_argument("--binding-sha256", required=True)
            narrow.add_argument("--audit-output", type=Path, required=True)
        if name == "bind-completed-shell":
            narrow.add_argument("--completion-receipt", type=Path, required=True)
            narrow.add_argument("--completion-receipt-sha256", required=True)
        if name == "bind-unslop-done":
            narrow.add_argument("--graph-cas", type=Path, required=True)
            narrow.add_argument("--graph-cas-sha256", required=True)
        if name == "bind-absent-manager-history":
            narrow.add_argument("--task", required=True)
            narrow.add_argument("--task-sha256", required=True)
            narrow.add_argument("--target", required=True)
        if name == "bind-source1385-live-worker-handoff":
            narrow.add_argument("--task-sha256", required=True)
            narrow.add_argument("--todo-sha256", required=True)
            narrow.add_argument("--successor-task", required=True)
            narrow.add_argument("--successor-target", required=True)
            narrow.add_argument("--successor-eligibility", type=Path, required=True)
            narrow.add_argument("--successor-eligibility-sha256", required=True)
    parsed = parser.parse_args(argv)
    for name in (
        "authority_sha256",
        "plan_sha256",
        "review_sha256",
        "code_review_sha256",
        "consumed_code_review_receipt_sha256",
        "binding_sha256",
        "completion_receipt_sha256",
        "graph_cas_sha256",
        "task_sha256",
        "todo_sha256",
        "successor_eligibility_sha256",
    ):
        value = getattr(parsed, name, None)
        if value is not None and SHA256_RE.fullmatch(value) is None:
            parser.error(f"--{name.replace('_', '-')} must be a lowercase SHA-256")
    return parsed


def require_executable_packet(command: str) -> None:
    if command in FROZEN_MUTATION_COMMANDS:
        raise DrainError(MUTABLE_HELPER_FROZEN_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        require_executable_packet(args.command)
        if args.command == "plan":
            value = build_plan(args.root.resolve(), tuple(args.prefix), args.authority_file.resolve(), args.authority_sha256, args.preparer)
            protected = {args.authority_file.resolve(), *args.root.resolve().rglob("*.md")}
            validate_new_private_output(args.output, args.root.resolve(), protected)
            atomic_write(args.output.resolve(), canonical_json(value))
            print(f"plan={args.output.resolve()} sha256={sha256_bytes(args.output.resolve().read_bytes())}")
        elif args.command == "apply":
            result = execute(
                args.plan.resolve(),
                args.plan_sha256,
                args.review.resolve(),
                args.review_sha256,
                args.ledger.resolve(),
                args.reviewer_task.resolve(),
                args.reviewer_task_sha256,
                args.consumed_review_receipt.resolve(),
                args.consumed_review_receipt_sha256,
                args.executor,
            )
            task_count = result.get("task_count", 0)
            print(f"ledger={args.ledger.resolve()} sha256={sha256_bytes(args.ledger.resolve().read_bytes())} tasks={task_count}")
        elif args.command == "bind-taskless-shell":
            result = bind_taskless_shell(
                args.root,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.output,
                args.executor,
            )
            print(f"binding={result['path']} sha256={result['sha256']}")
        elif args.command == "bind-completed-shell":
            result = bind_completed_shell(
                args.root,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.completion_receipt.resolve(),
                args.completion_receipt_sha256,
                args.output,
                args.executor,
            )
            print(f"binding={result['path']} sha256={result['sha256']}")
        elif args.command == "bind-unslop-done":
            result = bind_unslop_done(
                args.root,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.graph_cas.resolve(),
                args.graph_cas_sha256,
                args.output,
                args.executor,
            )
            print(f"binding={result['path']} sha256={result['sha256']}")
        elif args.command == "bind-absent-manager-history":
            result = bind_absent_manager_history(
                args.root,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.output,
                args.executor,
                args.task,
                args.task_sha256,
                args.target,
            )
            print(f"binding={result['path']} sha256={result['sha256']}")
        elif args.command == "bind-source1385-live-worker-handoff":
            result = bind_source1385_live_worker_handoff(
                args.root,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.output,
                args.executor,
                args.task_sha256,
                args.todo_sha256,
                args.successor_task,
                args.successor_target,
                args.successor_eligibility.resolve(),
                args.successor_eligibility_sha256,
            )
            print(f"binding={result['path']} sha256={result['sha256']}")
        elif args.command == "reconcile-taskless-shell":
            result = close_taskless_shell(
                args.root,
                args.binding.resolve(),
                args.binding_sha256,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.audit_output,
                args.executor,
            )
            print(f"audit={args.audit_output.absolute()} sha256={sha256_bytes(canonical_json(result))} pane={result['removed_pane_id']}")
        elif args.command == "reconcile-completed-shell":
            result = close_completed_shell(
                args.root,
                args.binding.resolve(),
                args.binding_sha256,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.audit_output,
                args.executor,
            )
            print(f"audit={args.audit_output.absolute()} sha256={sha256_bytes(canonical_json(result))} task={result['task']}")
        elif args.command == "reconcile-unslop-done":
            result = close_unslop_done(
                args.root,
                args.binding.resolve(),
                args.binding_sha256,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.audit_output,
                args.executor,
            )
            print(f"audit={args.audit_output.absolute()} sha256={sha256_bytes(canonical_json(result))} task={result['task']}")
        elif args.command == "reconcile-source1385-live-worker-handoff":
            result = close_source1385_live_worker_handoff(
                args.root,
                args.binding.resolve(),
                args.binding_sha256,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.audit_output,
                args.executor,
            )
            print(f"audit={args.audit_output.absolute()} sha256={sha256_bytes(canonical_json(result))}")
        else:
            result = close_absent_manager_history(
                args.root,
                args.binding.resolve(),
                args.binding_sha256,
                args.code_review.resolve(),
                args.code_review_sha256,
                args.consumed_code_review_receipt.resolve(),
                args.consumed_code_review_receipt_sha256,
                args.audit_output,
                args.executor,
            )
            print(f"audit={args.audit_output.absolute()} sha256={sha256_bytes(canonical_json(result))} task={result['task']}")
    except (DrainError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"omo_namespace_drain.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
