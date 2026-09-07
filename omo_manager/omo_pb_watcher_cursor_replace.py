#!/usr/bin/env python3
"""Replace the one PB news-watcher Cursor process in its existing tmux pane.

This helper is deliberately pinned to the live PB interpreter.  It preserves
the task and TODO bytes, proves sole task ownership, snapshots the database and
browser runtime, and respawns only the exact existing pane.  It never sends a
Gmail instruction and never reads or writes mail.
"""

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
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_agent_status import same_tmux_target
from omo_manager.omo_codex_status import (
    Args as StatusArgs,
    CURSOR_AGENT_EMPTY_INPUT_TEXTS,
    current_input_text,
    has_cursor_agent_running_indicator,
    has_cursor_followups_overlay,
    inspect,
    is_cursor_retained_submitted_composer,
)
from omo_manager.omo_manager_rotate import (
    ensure_private_directory,
    process_is_under,
    read_processes,
    write_private,
)
from omo_manager.omo_task import codex_cmd
from omo_manager.omo_task_lock import canonical_target, task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import authoritative_active_target_task_paths, root_membership_lock
from omo_manager.omo_tmux_input_lock import tmux_input_lock
from omo_manager.omo_worker_successor import (
    cursor_process_environment,
    cursor_runtime_identity,
    minimal_launch_environment,
    pinned_shell_identity,
    pinned_tmux_identity,
)

TASK_FILE = "202607/pbw_interpreter_live.md"
TARGET = "pb-newswatcher-agent:0.0"
MANAGER_TARGET = "pb:13"
WORKDIR = Path("/ssd1/sichangheagent/personal_browser_setup")
ENV_FILE = WORKDIR / "pb_watcher.env"
EXPECTED_STATUS = "long_running"
EXPECTED_TOOL = "cursor"
MODEL_KEY = "PB_WATCHER_AGENT_MODEL"
EFFORT_KEY = "PB_WATCHER_AGENT_REASONING_EFFORT"
DB_KEY = "PB_WATCHER_DB"
CDP_KEY = "BROWSER_USE_CDP_URL"
BROWSER_SESSION_KEYS = (
    "BROWSER_USE_TMUX_SESSION",
    "BROWSER_USE_CHROME_TMUX_SESSION",
    "BROWSER_USE_RESIZE_TMUX_SESSION",
    "BROWSER_USE_NOVNC_TMUX_SESSION",
)
REQUIRED_ENV_BINDINGS = {
    "PB_WATCHER_AGENT_TMUX": TARGET,
    "PB_WATCHER_AGENT_TASK_FILE": f"/ssd1/sichangheagent/work_logs/{TASK_FILE}",
    "PB_WATCHER_AGENT_TOOL": EXPECTED_TOOL,
}
AUTHORITY_FILE = Path("manager_mail/85c5dff58359-1468.txt")
AUTHORITY_LINES = (3, 7)
AUTHORITY_TEXT = """For manager, I don't know what the fuck this agent is having issue with.
Previously PB newswatch worked fine. Replace them or just close them if
there isn't an actual issue. If you do replace them, the new agent must
speak straightforward plain English and actually explain the problem in a
way humans can understand."""
SCHEMA = "omo-pb-watcher-cursor-replacement/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUCCESS_STATUSES = {"ready"}


class ReplaceError(RuntimeError):
    """A PB watcher replacement safety gate failed."""


@dataclass(frozen=True)
class Args:
    root: Path
    task_file: str
    target: str
    expected_binding_sha256: str
    audit_output: Path | None
    state_dir: Path
    startup_timeout_s: float
    poll_interval_s: float
    describe: bool
    reconcile_audit: bool = False
    expected_audit_sha256: str = ""


@dataclass(frozen=True)
class FileProof:
    path: str
    exists: bool
    device: int = 0
    inode: int = 0
    size: int = 0
    mtime_ns: int = 0
    mode: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class PaneProof:
    target: str
    session_id: str
    window_id: str
    pane_id: str
    pane_pid: int
    pane_start_ticks: int
    command: str
    workdir: str
    session_attached: bool = False


@dataclass(frozen=True)
class CursorProof:
    pid: int
    start_ticks: int
    executable: str
    argv_sha256: str


@dataclass(frozen=True)
class LifecycleProof:
    task: FileProof
    todo: FileProof
    status: str
    tool: str
    runat: str
    managerat: str
    pending_items_sha256: str
    pending_item_count: int
    gmail_marker_sha256: str
    authoritative_owner_count: int


@dataclass(frozen=True)
class ProtectedProof:
    environment: FileProof
    database: tuple[FileProof, ...]
    browser_panes: tuple[PaneProof, ...]
    cdp_version_sha256: str
    cdp_pages_sha256: str
    loop_session_present: bool


@dataclass(frozen=True)
class RuntimeProof:
    cursor_runtime_sha256: str
    tmux_runtime_sha256: str
    shell_runtime_sha256: str
    startup_mode: str
    model: str
    reasoning_effort: str
    process_environment_sha256: str


@dataclass(frozen=True)
class AuthorityProof:
    source: FileProof
    lines: tuple[int, int]
    excerpt_sha256: str


@dataclass(frozen=True)
class Binding:
    schema: str
    target_pane: PaneProof
    cursor: CursorProof
    retained_composer_sha256: str
    lifecycle: LifecycleProof
    protected: ProtectedProof
    runtime: RuntimeProof
    authority: AuthorityProof


class ParsedArgs(argparse.Namespace):
    root: Path
    task_file: str
    target: str
    expected_binding_sha256: str
    audit_output: Path | None
    state_dir: Path
    startup_timeout_s: float
    poll_interval_s: float
    describe: bool
    reconcile_audit: bool
    expected_audit_sha256: str


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--task-file", required=True)
    _ = parser.add_argument("--target", required=True)
    _ = parser.add_argument("--expected-binding-sha256", default="")
    _ = parser.add_argument("--audit-output", type=Path)
    _ = parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/omo-manager")
    _ = parser.add_argument("--startup-timeout-s", type=float, default=180.0)
    _ = parser.add_argument("--poll-interval-s", type=float, default=0.5)
    _ = parser.add_argument("--describe", action="store_true", help="Print the current authenticated binding without changing anything.")
    _ = parser.add_argument("--reconcile-audit", action="store_true", help="Verify and commit one exact prepared/completion-unknown audit without respawning.")
    _ = parser.add_argument("--expected-audit-sha256", default="")
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.task_file != TASK_FILE:
        parser.error(f"this helper is pinned to {TASK_FILE}")
    if parsed.target.partition(":")[0].startswith("h") or not same_tmux_target(parsed.target, TARGET):
        parser.error(f"this helper is pinned to non-human target {TARGET}")
    if parsed.root.expanduser().resolve(strict=False) != Path("/ssd1/sichangheagent/work_logs"):
        parser.error("this helper is pinned to /ssd1/sichangheagent/work_logs")
    if parsed.describe and parsed.reconcile_audit:
        parser.error("--describe and --reconcile-audit are mutually exclusive")
    if parsed.describe:
        if parsed.expected_binding_sha256 or parsed.audit_output is not None:
            parser.error("--describe does not accept execution or audit arguments")
        if parsed.expected_audit_sha256:
            parser.error("--describe does not accept --expected-audit-sha256")
    elif parsed.reconcile_audit:
        if parsed.expected_binding_sha256 or parsed.audit_output is None or SHA256_RE.fullmatch(parsed.expected_audit_sha256) is None:
            parser.error("reconciliation requires --audit-output and --expected-audit-sha256 only")
    elif SHA256_RE.fullmatch(parsed.expected_binding_sha256) is None or parsed.audit_output is None:
        parser.error("replacement requires --expected-binding-sha256 and --audit-output")
    elif parsed.expected_audit_sha256:
        parser.error("--expected-audit-sha256 is valid only with --reconcile-audit")
    if parsed.startup_timeout_s <= 0 or parsed.poll_interval_s <= 0:
        parser.error("timeouts must be positive")
    state_dir = parsed.state_dir.expanduser().resolve(strict=False)
    audit_output = parsed.audit_output.expanduser().resolve(strict=False) if parsed.audit_output else None
    if audit_output is not None and not audit_output.is_relative_to(state_dir):
        parser.error("--audit-output must be inside --state-dir")
    return Args(
        parsed.root.expanduser().resolve(strict=False),
        parsed.task_file,
        TARGET,
        parsed.expected_binding_sha256,
        audit_output,
        state_dir,
        parsed.startup_timeout_s,
        parsed.poll_interval_s,
        parsed.describe,
        parsed.reconcile_audit,
        parsed.expected_audit_sha256,
    )


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def file_proof(path: Path, *, allow_missing: bool = False) -> FileProof:
    """Hash one nonsymlink file while proving its identity did not change."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return FileProof(str(path), False)
        raise ReplaceError(f"required file is missing: {path}") from None
    except OSError as error:
        raise ReplaceError(f"could not open {path} safely: {error}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ReplaceError(f"required file is not one owner-owned regular file: {path}")
        hasher = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            hasher.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReplaceError(f"file disappeared while it was read: {path}: {error}") from error
    def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode

    if identity(before) != identity(after) or identity(after) != identity(current) or stat.S_ISLNK(current.st_mode):
        raise ReplaceError(f"file changed while it was authenticated: {path}")
    return FileProof(
        str(path),
        True,
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IMODE(after.st_mode),
        hasher.hexdigest(),
    )


def process_start_ticks(pid: int, proc_root: Path = Path("/proc")) -> int:
    try:
        fields = (proc_root / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        value = int(fields[19])
    except (IndexError, OSError, ValueError) as error:
        raise ReplaceError(f"could not authenticate process {pid} start time: {error}") from error
    if value <= 0:
        raise ReplaceError(f"process {pid} has an invalid start time")
    return value


def run_tmux(arguments: list[str], *, runtime: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    tmux_path = str(runtime["tmux_path"]) if runtime is not None else "tmux"
    environment = minimal_launch_environment() if runtime is not None else None
    return subprocess.run([tmux_path, *arguments], capture_output=True, text=True, timeout=15, check=False, env=environment)


def pane_proof(target: str, *, runtime: dict[str, str] | None = None) -> PaneProof:
    result = run_tmux(
        [
            "display-message",
            "-p",
            "-t",
            f"={target}",
            "#{session_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{session_attached}",
        ],
        runtime=runtime,
    )
    fields = result.stdout.rstrip("\r\n").split("\t") if result.returncode == 0 else []
    if len(fields) != 8 or not fields[4].isdigit() or fields[7] not in {"0", "1"} or not same_tmux_target(fields[1], target):
        raise ReplaceError(f"could not resolve exact pane identity for {target}")
    pid = int(fields[4])
    return PaneProof(fields[1], fields[0], fields[2], fields[3], pid, process_start_ticks(pid), fields[5], fields[6], fields[7] == "1")


def parse_env(proof: FileProof) -> dict[str, str]:
    data = Path(proof.path).read_bytes()
    if digest(data) != proof.sha256:
        raise ReplaceError("pb_watcher.env changed after it was authenticated")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReplaceError("pb_watcher.env is not UTF-8") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise ReplaceError("pb_watcher.env has malformed or duplicate fields")
        values[key] = value.strip('"')
    for key, expected in REQUIRED_ENV_BINDINGS.items():
        actual = values.get(key, "")
        if key == "PB_WATCHER_AGENT_TMUX":
            if not same_tmux_target(actual, expected):
                raise ReplaceError(f"pb_watcher.env {key} drifted")
        elif actual != expected:
            raise ReplaceError(f"pb_watcher.env {key} drifted")
    if not values.get(MODEL_KEY) or values.get(EFFORT_KEY) not in {"low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ReplaceError("pb_watcher.env has no supported Cursor model/effort binding")
    if values.get(CDP_KEY) != "http://127.0.0.1:9279":
        raise ReplaceError("pb_watcher.env CDP binding drifted from the protected local browser")
    return values


def live_marker(text: str) -> bytes:
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.strip() == "(pending)"]
    if len(starts) != 1:
        raise ReplaceError("task must contain exactly one live (pending) Gmail marker")
    start = starts[0]
    end = next((index + 1 for index in range(start + 1, len(lines)) if lines[index].startswith("(pending items recorded line=")), -1)
    if end < 0:
        raise ReplaceError("live Gmail marker lacks its durable pending-items record")
    marker = "".join(lines[start:end]).encode()
    if b"Gmail" not in marker or b"mail" not in marker:
        raise ReplaceError("the sole live pending marker is not the expected Gmail blocker")
    return marker


def authority_proof(args: Args) -> AuthorityProof:
    path = args.root / AUTHORITY_FILE
    source = file_proof(path)
    try:
        mail_root = path.parent.resolve(strict=True)
        root = args.root.resolve(strict=True)
    except OSError as error:
        raise ReplaceError(f"Human authority path is unavailable: {error}") from error
    info = mail_root.stat()
    if path.resolve(strict=True).parent != mail_root or mail_root != root / "manager_mail" or mail_root.is_symlink():
        raise ReplaceError("Human authority must be the pinned manager-mail source")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ReplaceError("Human authority directory must be owner-private")
    data = path.read_bytes()
    if digest(data) != source.sha256:
        raise ReplaceError("Human authority changed after it was authenticated")
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ReplaceError("Human authority is not UTF-8") from error
    start, end = AUTHORITY_LINES
    excerpt = "\n".join(lines[start - 1 : end])
    if excerpt != AUTHORITY_TEXT:
        raise ReplaceError("Human authority excerpt does not exactly authorize this replacement")
    return AuthorityProof(source, AUTHORITY_LINES, digest(excerpt.encode()))


def lifecycle_proof(args: Args) -> LifecycleProof:
    task_path = args.root / TASK_FILE
    task = file_proof(task_path)
    todo = file_proof(args.root / "TODO.md")
    task_data = task_path.read_bytes()
    todo_data = (args.root / "TODO.md").read_bytes()
    if digest(task_data) != task.sha256 or digest(todo_data) != todo.sha256:
        raise ReplaceError("task or TODO changed after it was authenticated")
    try:
        text = task_data.decode("utf-8")
        todo_text = todo_data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReplaceError("task or TODO is not UTF-8") from error
    try:
        metadata = parse_task_metadata(text, args.root)
    except TaskFrontmatterError as error:
        raise ReplaceError(f"PB watcher task frontmatter is invalid: {error}") from error
    if metadata is None:
        raise ReplaceError("PB watcher task has no frontmatter")
    if (
        metadata.status != EXPECTED_STATUS
        or metadata.tool != EXPECTED_TOOL
        or not same_tmux_target(metadata.runat, TARGET)
        or not same_tmux_target(metadata.managerat, MANAGER_TARGET)
        or metadata.is_manager
        or not metadata.pending_task_items
    ):
        raise ReplaceError("PB watcher task status/tool/target/manager/queue binding drifted")
    expected_todo = f"{TASK_FILE} {metadata.runat}"
    todo_matches = [line.strip() for line in todo_text.splitlines() if line.strip().startswith(f"{TASK_FILE} ")]
    if todo_matches != [expected_todo]:
        raise ReplaceError("TODO.md does not contain exactly one bound PB watcher entry")
    owners = authoritative_active_target_task_paths(args.root, TARGET)
    if owners != (task_path.resolve(),):
        raise ReplaceError("PB watcher pane does not have exactly one authoritative active task owner")
    marker = live_marker(text)
    return LifecycleProof(
        task,
        todo,
        metadata.status,
        metadata.tool,
        metadata.runat,
        metadata.managerat,
        digest("\0".join(metadata.pending_task_items).encode()),
        len(metadata.pending_task_items),
        digest(marker),
        1,
    )


def cdp_json_digest(url: str, endpoint: str) -> str:
    try:
        with urlopen(f"{url.rstrip('/')}/{endpoint.lstrip('/')}", timeout=3) as response:
            data = response.read(1024 * 1024 + 1)
    except (OSError, URLError) as error:
        raise ReplaceError(f"protected browser CDP {endpoint} endpoint is unavailable: {error}") from error
    if not data or len(data) > 1024 * 1024:
        raise ReplaceError(f"protected browser CDP {endpoint} response is empty or oversized")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplaceError(f"protected browser CDP {endpoint} response is invalid") from error
    if endpoint == "json/version":
        if not isinstance(value, dict) or not str(value.get("Browser", "")).startswith("Chrome/") or not str(value.get("webSocketDebuggerUrl", "")).startswith("ws://127.0.0.1:9279/devtools/browser/"):
            raise ReplaceError("protected browser CDP identity is ambiguous")
    elif not isinstance(value, list) or any(not isinstance(item, dict) or not all(key in item for key in ("id", "type", "url")) for item in value):
        raise ReplaceError("protected browser CDP page inventory is ambiguous")
    if endpoint == "json/list":
        stable_pages = sorted(
            ({key: str(item.get(key, "")) for key in ("id", "type", "url", "webSocketDebuggerUrl")} for item in value),
            key=lambda item: (item["id"], item["type"], item["url"]),
        )
        return digest(canonical_json(stable_pages))
    return digest(data)


def loop_session_present(runtime: dict[str, str]) -> bool:
    result = run_tmux(["has-session", "-t", "=pb-watch-loop"], runtime=runtime)
    return result.returncode == 0


def protected_proof(environment: FileProof, values: dict[str, str], tmux_runtime: dict[str, str]) -> ProtectedProof:
    db = Path(values[DB_KEY]).resolve(strict=False)
    if db != Path("/ssd1/sichangheagent/data/pb_watcher.sqlite"):
        raise ReplaceError("PB watcher database binding drifted")
    database = tuple(file_proof(Path(f"{db}{suffix}"), allow_missing=suffix != "") for suffix in ("", "-wal", "-shm"))
    browser_targets = tuple(f"{values[key]}:0.0" for key in BROWSER_SESSION_KEYS)
    if len(set(browser_targets)) != len(browser_targets) or any(target.partition(":")[0].startswith("h") for target in browser_targets):
        raise ReplaceError("protected browser tmux target set is invalid")
    panes = tuple(pane_proof(target, runtime=tmux_runtime) for target in browser_targets)
    return ProtectedProof(
        environment,
        database,
        panes,
        cdp_json_digest(values[CDP_KEY], "json/version"),
        cdp_json_digest(values[CDP_KEY], "json/list"),
        loop_session_present(tmux_runtime),
    )


def runtime_proof(args: Args, values: dict[str, str], cursor_runtime: dict[str, object], tmux_runtime: dict[str, str]) -> RuntimeProof:
    shell_runtime = pinned_shell_identity()
    process_environment = cursor_process_environment(
        workdir=WORKDIR,
        target=canonical_target(TARGET),
        amh_caller_agent="",
        runtime=cursor_runtime,  # type: ignore[arg-type]
    )
    process_environment["OMO_WORK_LOGS_ROOT"] = str(args.root)
    return RuntimeProof(
        digest(canonical_json(cursor_runtime)),
        digest(canonical_json(tmux_runtime)),
        digest(canonical_json(shell_runtime)),
        "fresh-no-prompt",
        values[MODEL_KEY],
        values[EFFORT_KEY],
        digest(canonical_json(process_environment)),
    )


def cursor_candidates(
    pane: PaneProof,
    runtime: dict[str, object],
    expected_prompt: bytes | None = None,
    expected_model: str = "",
) -> list[CursorProof]:
    try:
        processes = read_processes()
    except Exception as error:
        raise ReplaceError(f"could not inspect Cursor processes: {error}") from error
    launcher_paths = {str(runtime["launcher_path"]), str(runtime["launcher_resolved"])}
    index = str(runtime["index_path"])
    node = Path(str(runtime["node_path"]))
    matches: list[CursorProof] = []
    for process in processes.values():
        if process.state == "Z" or not process.argv or not process_is_under(process.pid, pane.pane_pid, processes):
            continue
        argv = process.argv
        if argv[0] not in launcher_paths:
            continue
        index_at = 2 if len(argv) > 2 and argv[1] == "--use-system-ca" else 1
        if len(argv) <= index_at or argv[index_at] != index:
            continue
        tail = argv[index_at + 1 :]
        required = ("--force", "--sandbox", "disabled", "--trust", "--workspace", str(WORKDIR), "--model")
        if len(tail) < len(required) + 1 or tail[: len(required)] != required or "--resume" in tail:
            continue
        remainder = tail[len(required) :]
        if expected_model and remainder[0] != expected_model:
            continue
        if expected_prompt is not None:
            if expected_prompt:
                try:
                    prompt = expected_prompt.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ReplaceError("replacement prompt is not UTF-8") from error
                exact_remainder = (remainder[0], prompt)
            else:
                exact_remainder = (remainder[0],)
            if remainder != exact_remainder:
                continue
        try:
            executable = (Path("/proc") / str(process.pid) / "exe").resolve(strict=True)
        except OSError:
            continue
        if executable != node:
            continue
        matches.append(CursorProof(process.pid, process_start_ticks(process.pid), str(executable), digest(b"\0".join(part.encode() for part in argv))))
    return matches


def require_isolated_pane_process_tree(pane: PaneProof, cursor: CursorProof) -> None:
    """Allow only the exact pane-to-Cursor ancestry, never sibling work."""

    processes = read_processes()
    descendants = {
        process.pid
        for process in processes.values()
        if process.state != "Z" and process_is_under(process.pid, pane.pane_pid, processes)
    }
    ancestry: set[int] = set()
    current = cursor.pid
    while True:
        process = processes.get(current)
        if process is None or process.state == "Z":
            raise ReplaceError("PB watcher pane process ancestry is incomplete")
        ancestry.add(current)
        if current == pane.pane_pid:
            break
        current = process.ppid
        if current <= 1 or current in ancestry:
            raise ReplaceError("PB watcher Cursor is not in one exact pane ancestry")
    if descendants != ancestry:
        raise ReplaceError("PB watcher pane contains unbound sibling or child work")


def capture_lines(target: str, tmux_runtime: dict[str, str]) -> list[str]:
    result = run_tmux(["capture-pane", "-p", "-t", f"={target}", "-S", "-500"], runtime=tmux_runtime)
    if result.returncode != 0:
        raise ReplaceError("could not capture the watcher pane for retained-composer authentication")
    return result.stdout.splitlines()


def capture_binding(
    args: Args,
    *,
    require_retained: bool = True,
    allow_replacement_command: bool = False,
) -> tuple[Binding, dict[str, str], dict[str, object], dict[str, str]]:
    runtime = cursor_runtime_identity()
    tmux_runtime = pinned_tmux_identity()
    environment = file_proof(ENV_FILE)
    values = parse_env(environment)
    target = pane_proof(TARGET, runtime=tmux_runtime)
    expected_commands = {"agent"}
    if allow_replacement_command:
        expected_commands.add(Path(str(runtime["launcher_resolved"])).name)
    if target.command not in expected_commands or target.session_attached or Path(target.workdir).resolve(strict=False) != WORKDIR:
        raise ReplaceError("PB watcher target is not the exact live Cursor pane in the protected workdir")
    candidates = cursor_candidates(target, runtime)
    if len(candidates) != 1:
        raise ReplaceError("PB watcher pane does not contain exactly one authenticated Cursor process")
    lines = capture_lines(TARGET, tmux_runtime)
    composer = current_input_text(lines)
    if require_retained and (not is_cursor_retained_submitted_composer(lines) or not composer):
        raise ReplaceError("PB watcher no longer has the exact retained submitted-composer defect")
    if has_cursor_followups_overlay(lines):
        raise ReplaceError("PB watcher has a follow-up overlay; replacement ownership is ambiguous")
    require_isolated_pane_process_tree(target, candidates[0])
    lifecycle = lifecycle_proof(args)
    protected = protected_proof(environment, values, tmux_runtime)
    runtime_binding = runtime_proof(args, values, runtime, tmux_runtime)
    authority = authority_proof(args)
    return Binding(SCHEMA, target, candidates[0], digest(composer.encode()), lifecycle, protected, runtime_binding, authority), values, runtime, tmux_runtime


def binding_sha256(binding: Binding) -> str:
    return digest(canonical_json(asdict(binding)))


def require_no_legacy_sender(target: str) -> None:
    """Reject a sender that began before target-input locking was available."""

    for process in read_processes().values():
        if process.pid == os.getpid() or process.state == "Z" or not process.argv:
            continue
        if any(Path(argument).name == "omo_tmux_send.py" for argument in process.argv) and any(
            same_tmux_target(argument, target) for argument in process.argv if ":" in argument
        ):
            raise ReplaceError("a pre-lock supported sender is still active for the PB watcher target")


def verify_unchanged(expected: Binding, current: Binding, *, after_replacement: bool = False) -> None:
    if current.lifecycle != expected.lifecycle or current.protected != expected.protected or current.runtime != expected.runtime or current.authority != expected.authority:
        raise ReplaceError("task, TODO, queue, Gmail marker, database, browser, or loop state changed")
    if not after_replacement and current != expected:
        raise ReplaceError("watcher pane/process/composer binding changed before replacement")


def replacement_command(
    args: Args,
    pane: PaneProof,
    values: dict[str, str],
    runtime: dict[str, object],
) -> tuple[str, dict[str, str]]:
    shell = pinned_shell_identity()
    command = codex_cmd(
        tool="cursor",
        model=values[MODEL_KEY],
        reasoning_effort=values[EFFORT_KEY],
        workdir=WORKDIR,
        include_prompt=False,
        cursor_runtime=Path(str(runtime["launcher_resolved"])),
    )
    canonical = canonical_target(TARGET)
    process_environment = cursor_process_environment(workdir=WORKDIR, target=canonical, amh_caller_agent="", runtime=runtime)  # type: ignore[arg-type]
    process_environment["OMO_WORK_LOGS_ROOT"] = str(args.root)
    launch_environment = minimal_launch_environment()
    launch_environment.update({"OMO_AGENT_TMUX_TARGET": canonical, "OMO_WORK_LOGS_ROOT": str(args.root)})
    assignments = [f"{key}={value}" for key, value in sorted(launch_environment.items())]
    # tmux respawn-pane supplies the bound working directory.  Avoid `cd` here:
    # env-empty bash would otherwise export an unbound OLDPWD to Cursor.
    inner = f"exec {command}"
    rendered = "exec " + shlex.join([str(shell["env_path"]), "-i", *assignments, str(shell["bash_path"]), "--noprofile", "--norc", "-c", inner])
    if "--resume" in rendered:
        raise ReplaceError("replacement command unexpectedly resumes an old Cursor session")
    return rendered, process_environment


def atomic_respawn(old: PaneProof, command: str, tmux_runtime: dict[str, str]) -> None:
    condition = "#{&&:#{==:#{session_id},%s},#{==:#{window_id},%s},#{==:#{pane_id},%s},#{==:#{session_name}:#{window_index}.#{pane_index},%s},#{==:#{pane_pid},%s},#{==:#{pane_current_command},%s}}" % (
        old.session_id,
        old.window_id,
        old.pane_id,
        old.target,
        old.pane_pid,
        old.command,
    )
    respawn = "respawn-pane -k -t %s -c %s %s" % (shlex.quote(old.pane_id), shlex.quote(old.workdir), shlex.quote(command))
    result = run_tmux(["if-shell", "-F", "-t", old.pane_id, condition, respawn, ""], runtime=tmux_runtime)
    if result.returncode != 0:
        raise ReplaceError(f"exact guarded pane respawn failed: {result.stderr.strip() or 'tmux rejected the operation'}")
    current = pane_proof(TARGET, runtime=tmux_runtime)
    if (current.session_id, current.window_id, current.pane_id) != (old.session_id, old.window_id, old.pane_id) or current.pane_pid == old.pane_pid:
        raise ReplaceError("completion-unknown: guarded respawn did not prove the same pane with a new process")


def exact_process_environment(proof: CursorProof) -> dict[str, str]:
    try:
        raw = (Path("/proc") / str(proof.pid) / "environ").read_bytes()
    except OSError as error:
        raise ReplaceError(f"could not authenticate replacement Cursor environment: {error}") from error
    result: dict[str, str] = {}
    try:
        for item in raw.rstrip(b"\0").split(b"\0"):
            key, separator, value = item.partition(b"=")
            decoded = key.decode()
            if not separator or not decoded or decoded in result:
                raise ReplaceError("replacement Cursor environment is malformed or duplicated")
            result[decoded] = value.decode()
    except UnicodeDecodeError as error:
        raise ReplaceError("replacement Cursor environment is not UTF-8") from error
    return result


def wait_ready_empty(
    old: PaneProof,
    expected_prompt: bytes,
    expected_model: str,
    expected_environment: dict[str, str],
    runtime: dict[str, object],
    tmux_runtime: dict[str, str],
    timeout_s: float,
    poll_interval_s: float,
) -> tuple[PaneProof, CursorProof]:
    deadline = time.monotonic() + timeout_s
    last = "starting"
    while time.monotonic() < deadline:
        current = pane_proof(TARGET, runtime=tmux_runtime)
        if (current.session_id, current.window_id, current.pane_id) != (old.session_id, old.window_id, old.pane_id) or current.pane_pid == old.pane_pid:
            raise ReplaceError("replacement changed the tmux session/window/pane binding")
        if current.session_attached:
            raise ReplaceError("replacement tmux session became attached")
        if current.command != Path(str(runtime["launcher_resolved"])).name:
            raise ReplaceError("replacement pane is not running Cursor Agent")
        if cursor_runtime_identity() != runtime or pinned_tmux_identity() != tmux_runtime:
            raise ReplaceError("Cursor or tmux runtime changed during replacement startup")
        all_candidates = cursor_candidates(current, runtime)
        candidates = cursor_candidates(current, runtime, expected_prompt, expected_model)
        if len(all_candidates) != 1:
            raise ReplaceError("replacement pane does not contain exactly one total Cursor process")
        if len(candidates) == 1 and candidates[0] == all_candidates[0]:
            candidate = all_candidates[0]
            if exact_process_environment(candidate) != expected_environment:
                raise ReplaceError("replacement Cursor process did not inherit the exact sanitized environment")
            report = inspect(StatusArgs(TARGET, 500))
            lines = capture_lines(TARGET, tmux_runtime)
            input_text = current_input_text(lines)
            last = report.status
            if report.status in SUCCESS_STATUSES:
                if (
                    input_text in CURSOR_AGENT_EMPTY_INPUT_TEXTS
                    and not is_cursor_retained_submitted_composer(lines)
                    and not has_cursor_followups_overlay(lines)
                    and not has_cursor_agent_running_indicator(lines)
                ):
                    return current, candidate
                last = "ready-but-composer-not-empty"
            elif report.status == "error":
                raise ReplaceError("replacement Cursor startup entered an error state")
        time.sleep(min(poll_interval_s, max(0.01, deadline - time.monotonic())))
    raise ReplaceError(f"replacement did not become ready with an empty composer: {last}")


def reserve_audit(path: Path, binding: Binding, binding_digest: str) -> bytes:
    ensure_private_directory(path.parent)
    record = {
        "schema": SCHEMA,
        "state": "prepared",
        "binding_sha256": binding_digest,
        "target": binding.target_pane.target,
        "session_id": binding.target_pane.session_id,
        "window_id": binding.target_pane.window_id,
        "pane_id": binding.target_pane.pane_id,
        "old_pane_pid": binding.target_pane.pane_pid,
        "old_pane_start_ticks": binding.target_pane.pane_start_ticks,
        "old_cursor_pid": binding.cursor.pid,
        "old_cursor_start_ticks": binding.cursor.start_ticks,
        "target_pane": asdict(binding.target_pane),
        "cursor": asdict(binding.cursor),
        "task_sha256": binding.lifecycle.task.sha256,
        "todo_sha256": binding.lifecycle.todo.sha256,
        "pending_items_sha256": binding.lifecycle.pending_items_sha256,
        "gmail_marker_sha256": binding.lifecycle.gmail_marker_sha256,
        "protected_sha256": digest(canonical_json(asdict(binding.protected))),
        "lifecycle": asdict(binding.lifecycle),
        "protected": asdict(binding.protected),
        "runtime": asdict(binding.runtime),
        "authority": asdict(binding.authority),
        "prompt_delivery": "no-startup-prompt; gmail-held-for-manager",
    }
    data = canonical_json(record)
    write_private(path, data.decode())
    return data


def finish_audit(path: Path, prepared: bytes, state: str, *, new_pane: PaneProof | None = None, new_cursor: CursorProof | None = None, detail: str = "") -> None:
    current = file_proof(path)
    if current.sha256 != digest(prepared) or Path(path).read_bytes() != prepared or current.mode != 0o600:
        raise ReplaceError("private replacement audit changed before finalization")
    record = json.loads(prepared)
    record["state"] = state
    if new_pane is not None and new_cursor is not None:
        record.update(
            {
                "new_pane_pid": new_pane.pane_pid,
                "new_pane_start_ticks": new_pane.pane_start_ticks,
                "new_cursor_pid": new_cursor.pid,
                "new_cursor_start_ticks": new_cursor.start_ticks,
                "new_cursor_argv_sha256": new_cursor.argv_sha256,
                "terminal_authoritative_owner_count": 1,
                "terminal_composer": "empty",
            }
        )
    if detail:
        record["detail"] = detail
    data = canonical_json(record)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if Path(path).read_bytes() != prepared:
            raise ReplaceError("private replacement audit changed before atomic finalization")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_reconcilable_audit(path: Path, expected_sha256: str) -> tuple[bytes, dict[str, object]]:
    proof = file_proof(path)
    if proof.mode != 0o600 or proof.sha256 != expected_sha256:
        raise ReplaceError("replacement audit mode or SHA-256 does not match reconciliation authority")
    data = path.read_bytes()
    if digest(data) != expected_sha256:
        raise ReplaceError("replacement audit changed after it was authenticated")
    try:
        record = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReplaceError("replacement audit is not canonical JSON") from error
    if canonical_json(record) != data:
        raise ReplaceError("replacement audit is not canonical JSON")
    if not isinstance(record, dict) or record.get("schema") != SCHEMA or record.get("state") not in {"prepared", "respawn-attempted", "completion-unknown"}:
        raise ReplaceError("replacement audit is not one reconcilable incomplete transaction")
    required = {"binding_sha256", "target_pane", "cursor", "lifecycle", "protected", "runtime", "authority", "prompt_delivery"}
    if not required.issubset(record) or record.get("prompt_delivery") != "no-startup-prompt; gmail-held-for-manager":
        raise ReplaceError("replacement audit lacks its exact lifecycle/runtime binding")
    return data, record


def reconcile_replacement(args: Args) -> str:
    audit_path = args.audit_output
    if audit_path is None:
        raise ReplaceError("reconciliation requires an audit output")
    with tmux_input_lock(TARGET), root_membership_lock(args.root), task_target_lock(args.root, TARGET), ExitStack() as locks:
        require_no_legacy_sender(TARGET)
        for path in sorted((args.root / TASK_FILE, args.root / "TODO.md"), key=str):
            locks.enter_context(task_file_lock(path))
        audit_data, record = read_reconcilable_audit(audit_path, args.expected_audit_sha256)
        try:
            old_pane = PaneProof(**record["target_pane"])  # type: ignore[arg-type]
            old_cursor = CursorProof(**record["cursor"])  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise ReplaceError("replacement audit has invalid old pane/process evidence") from error
        current, values, runtime, tmux_runtime = capture_binding(
            args,
            require_retained=False,
            allow_replacement_command=True,
        )
        if (
            canonical_json(asdict(current.lifecycle)) != canonical_json(record["lifecycle"])
            or canonical_json(asdict(current.protected)) != canonical_json(record["protected"])
            or canonical_json(asdict(current.runtime)) != canonical_json(record["runtime"])
            or canonical_json(asdict(current.authority)) != canonical_json(record["authority"])
        ):
            raise ReplaceError("reconciliation found lifecycle, queue, database, browser, or runtime drift")
        if binding_sha256(current) == record["binding_sha256"]:
            finish_audit(audit_path, audit_data, "aborted-no-respawn")
            return f"reconciled {TARGET} as aborted-no-respawn; the exact original worker remains and no input was sent; audit={audit_path}"
        if (
            (current.target_pane.session_id, current.target_pane.window_id, current.target_pane.pane_id)
            != (old_pane.session_id, old_pane.window_id, old_pane.pane_id)
            or current.target_pane.pane_pid == old_pane.pane_pid
        ):
            raise ReplaceError("reconciliation does not see the exact same pane with a replacement process")
        expected_prompt = b""
        expected_model = f"{values[MODEL_KEY]}-{values[EFFORT_KEY]}"
        expected_environment = cursor_process_environment(
            workdir=WORKDIR,
            target=canonical_target(TARGET),
            amh_caller_agent="",
            runtime=runtime,  # type: ignore[arg-type]
        )
        expected_environment["OMO_WORK_LOGS_ROOT"] = str(args.root)
        new_pane, new_cursor = wait_ready_empty(
            old_pane,
            expected_prompt,
            expected_model,
            expected_environment,
            runtime,
            tmux_runtime,
            args.startup_timeout_s,
            args.poll_interval_s,
        )
        try:
            old_alive = process_start_ticks(old_cursor.pid) == old_cursor.start_ticks
        except ReplaceError:
            old_alive = False
        if old_alive or new_cursor.pid == old_cursor.pid:
            raise ReplaceError("reconciliation cannot prove distinct old and new Cursor processes")
        if authoritative_active_target_task_paths(args.root, TARGET) != ((args.root / TASK_FILE).resolve(),):
            raise ReplaceError("reconciliation cannot prove exactly one authoritative task owner")
        require_isolated_pane_process_tree(new_pane, new_cursor)
        finish_audit(audit_path, audit_data, "committed", new_pane=new_pane, new_cursor=new_cursor)
        return f"reconciled {TARGET}; one task owner remains and the replacement composer is empty; audit={audit_path}"


def replace_watcher(args: Args) -> str:
    if args.reconcile_audit:
        return reconcile_replacement(args)
    if os.environ.get("TMUX_PANE"):
        try:
            current_pane = run_tmux(["display-message", "-p", "#{pane_id}"]).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            current_pane = ""
        if current_pane and current_pane == pane_proof(TARGET).pane_id:
            raise ReplaceError("run this helper from a different pane than the PB watcher")
    with tmux_input_lock(TARGET), root_membership_lock(args.root), task_target_lock(args.root, TARGET), ExitStack() as locks:
        require_no_legacy_sender(TARGET)
        for path in sorted((args.root / TASK_FILE, args.root / "TODO.md"), key=str):
            locks.enter_context(task_file_lock(path))
        binding, values, runtime, tmux_runtime = capture_binding(args)
        current_digest = binding_sha256(binding)
        if args.describe:
            print(json.dumps({"binding_sha256": current_digest, "binding": asdict(binding)}, indent=2, sort_keys=True))
            return ""
        if current_digest != args.expected_binding_sha256:
            raise ReplaceError("current authenticated binding does not equal --expected-binding-sha256")
        audit_path = args.audit_output
        if audit_path is None:
            raise ReplaceError("replacement requires an audit output")
        ensure_private_directory(args.state_dir)
        expected_prompt = b""
        expected_model = f"{values[MODEL_KEY]}-{values[EFFORT_KEY]}"
        command, expected_environment = replacement_command(args, binding.target_pane, values, runtime)
        prepared = reserve_audit(audit_path, binding, current_digest)
        active_audit = prepared
        respawned = False
        try:
            latest, _, _, _ = capture_binding(args)
            verify_unchanged(binding, latest)
            finish_audit(audit_path, active_audit, "respawn-attempted")
            respawned = True
            active_audit = audit_path.read_bytes()
            atomic_respawn(binding.target_pane, command, tmux_runtime)
            new_pane, new_cursor = wait_ready_empty(
                binding.target_pane,
                expected_prompt,
                expected_model,
                expected_environment,
                runtime,
                tmux_runtime,
                args.startup_timeout_s,
                args.poll_interval_s,
            )
            try:
                old_cursor_still_exists = process_start_ticks(binding.cursor.pid) == binding.cursor.start_ticks
            except ReplaceError:
                old_cursor_still_exists = False
            if old_cursor_still_exists or new_cursor.pid == binding.cursor.pid:
                raise ReplaceError("old and new Cursor process identities are not distinct")
            lifecycle = lifecycle_proof(args)
            environment = file_proof(ENV_FILE)
            protected = protected_proof(environment, parse_env(environment), tmux_runtime)
            if lifecycle != binding.lifecycle or protected != binding.protected:
                raise ReplaceError("protected lifecycle, queue, database, browser, or loop state changed after replacement")
            owners = authoritative_active_target_task_paths(args.root, TARGET)
            if owners != ((args.root / TASK_FILE).resolve(),):
                raise ReplaceError("replacement did not retain exactly one authoritative task owner")
            require_isolated_pane_process_tree(new_pane, new_cursor)
            finish_audit(audit_path, active_audit, "committed", new_pane=new_pane, new_cursor=new_cursor)
            return f"replaced {TARGET} in place; one task owner remains and the new Cursor composer is empty; audit={audit_path}"
        except Exception as error:
            state = "completion-unknown" if respawned else "failed-before-respawn"
            try:
                finish_audit(audit_path, active_audit, state, detail=str(error))
            except Exception as audit_error:
                error.add_note(f"audit finalization also failed: {audit_error}")
            if isinstance(error, ReplaceError):
                raise
            raise ReplaceError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        result = replace_watcher(args)
        if result:
            print(result)
    except (OSError, ReplaceError, subprocess.SubprocessError, ValueError) as error:
        print(f"omo_pb_watcher_cursor_replace.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
