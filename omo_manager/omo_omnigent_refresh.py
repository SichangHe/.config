"""Reviewed, one-shot server-only refresh; never stop the paper-owner host."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from contextlib import closing
from pathlib import Path
from threading import Thread
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from omo_manager.omo_manager_replace import ReplaceError, Snapshot, create_snapshot, metadata, read_snapshot, replace_snapshot
from omo_manager.omo_omnigent_backend import Process, pidfd_open, pidfd_signal, process, process_domain, read_config, same_process
from omo_manager.omo_omnigent_fence import OMNIGENT_SOURCE_SHA256, Rejected, canonical, digest
from omo_manager.omo_omnigent_replace import AUTHORITY_SHA256, OLD_HOST_ID, OLD_SESSION_ID, OLD_THREAD_ID, TASK_NAME, WORKSPACE_NAME, FilePin, ProcessPin, configuration_source, configured_workspace, implementation_sources, raw_queue
from omo_manager.omo_omnigent_server import BootstrapRejected, validate_package
from omo_manager.omo_task_lock import task_file_lock


@dataclass(frozen=True)
class RefreshRequest:
    server_pid: int
    checkout: str
    published_commit: str
    published_ref: str
    authority_path: str
    task_path: str
    todo_path: str
    workspace: str
    old_native_cwd: str
    old_bridge_cwd: str | None
    global_config_path: str
    fence_database: str
    control_socket: str
    preparation_path: str
    packet_path: str
    approval_path: str
    backend_config_path: str
    journal_path: str
    log_path: str


@dataclass(frozen=True)
class PreservedState:
    processes: tuple[ProcessPin, ...]
    database_sha256: str
    native_thread_sha256: str
    bridge_sha256: str
    public_sha256: str
    runner_id: str


@dataclass(frozen=True)
class RefreshPlan:
    request: RefreshRequest
    old_server: ProcessPin
    executable: str
    interpreter_sha256: str
    old_argv: tuple[str, ...]
    new_argv: tuple[str, ...]
    files: tuple[FilePin, ...]
    sources_sha256: str
    package_sha256: str
    queue_sha256: str
    before: PreservedState
    old_environment_sha256: str = ""


@dataclass(frozen=True)
class Journal:
    plan_sha256: str
    review_sha256: str
    phase: Literal["launch_intent", "standby", "stopping", "starting", "verified", "rejected"]
    child: ProcessPin | None = None
    detail: str = ""


def _bytes(value: object) -> bytes:
    return canonical(value).encode()


def _private(path: Path, *, required: bool) -> Rejected | None:
    try:
        parent = path.parent.stat()
        if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            return Rejected("unsafe_refresh_path", "refresh artifacts require an existing canonical owned 0700 directory")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return Rejected("missing_refresh_file", path.name) if required else None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            return Rejected("unsafe_refresh_path", "refresh files require regular singly linked owned 0600 files")
    except OSError as exc:
        return Rejected("unsafe_refresh_path", str(exc))
    return None


def _snapshot(path: str) -> FilePin:
    resolved = Path(path)
    if not resolved.is_absolute() or resolved.resolve(strict=True) != resolved:
        raise ValueError("pinned inputs must be existing canonical absolute paths")
    return FilePin.capture(read_snapshot(resolved, resolved.name))


def _source_hash(checkout: Path) -> str:
    paths = (*implementation_sources(), Path(__file__).resolve())
    values = [(str(path.relative_to(checkout)), digest(path.read_bytes())) for path in sorted(set(paths))]
    return digest(_bytes(values))


def _published(request: RefreshRequest) -> Rejected | None:
    """Require clean code at the exact reviewed commit also advertised by origin."""
    checkout = Path(request.checkout)
    try:
        if checkout.resolve(strict=True) != checkout or Path(__file__).resolve().parent.parent != checkout:
            return Rejected("wrong_checkout", "run the helper from the canonical published checkout")
        if len(request.published_commit) != 40 or not all(value in "0123456789abcdef" for value in request.published_commit) or not request.published_ref.startswith("refs/heads/"):
            return Rejected("invalid_publication", "an exact commit and full remote branch ref are required")
        def git(*args: str) -> str:
            result = subprocess.run(("git", "-C", str(checkout), *args), capture_output=True, text=True, timeout=30, check=True)
            return result.stdout.strip()
        if git("rev-parse", "HEAD") != request.published_commit or git("status", "--porcelain", "--untracked-files=all"):
            return Rejected("unpublished_sources", "checkout must be clean at the reviewed commit")
        if git("ls-remote", "--exit-code", "origin", request.published_ref) != f"{request.published_commit}\t{request.published_ref}":
            return Rejected("unpublished_sources", "origin does not advertise the reviewed commit at the reviewed ref")
    except (OSError, ValueError, subprocess.SubprocessError):
        return Rejected("publication_unobservable", "cannot authenticate clean published source")
    return None


def _old_command(value: Process) -> tuple[str, str, int] | Rejected:
    args = value.argv
    if value.uid != os.geteuid() or value.state in {"Z", "T", "t"} or len(args) != 12 or args[1:4] != ("-m", "omnigent.cli", "server") or args[4:7] != ("--host", "127.0.0.1", "--port") or args[8] != "--database-uri" or args[10] != "--artifact-location":
        return Rejected("unexpected_server", "only the exact original loopback CLI deployment is supported")
    try:
        port = int(args[7])
        if not 1 <= port <= 65535 or not args[9].startswith("sqlite:////") or not Path(args[11]).is_absolute():
            raise ValueError("invalid explicit storage or port")
        if Path(args[0]).resolve(strict=True) != Path(sys.executable).resolve(strict=True):
            return Rejected("wrong_interpreter", "run this helper with the original server interpreter")
        return args[9], args[11], port
    except (OSError, ValueError):
        return Rejected("unexpected_server", "cannot authenticate original interpreter/storage")


def _listens(pid: int, port: int) -> bool:
    """Bind the loopback listening socket to the observed process, not just a URL."""
    sockets = {os.readlink(path) for path in (Path("/proc") / str(pid) / "fd").iterdir()}
    rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
    matches = [row.split()[9] for row in rows if row.split()[1] == f"0100007F:{port:04X}" and row.split()[3] == "0A"]
    return len(matches) == 1 and f"socket:[{matches[0]}]" in sockets


def _get(url: str, header: bool = False) -> object:
    headers = {"X-Forwarded-Email": "local"} if header else {}
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def _public(url: str, runner_id: str) -> str | Rejected:
    info, me, header_me = _get(f"{url}/v1/info"), _get(f"{url}/v1/me"), _get(f"{url}/v1/me", True)
    runners, hosts = _get(f"{url}/v1/runners"), _get(f"{url}/v1/hosts")
    if not isinstance(info, dict) or info.get("single_user") is not True or info.get("accounts_enabled") is not False or info.get("server_version") != "0.11.0" or me != {"user_id": "local", "is_admin": True} or header_me != {"user_id": None, "is_admin": False}:
        return Rejected("effective_auth_mismatch", "expected local single-user/default-header auth is not observable")
    if not isinstance(runners, dict) or not isinstance(hosts, dict):
        return Rejected("routing_unobservable", "runner/host inventory is unavailable")
    values = runners.get("data")
    host_values = hosts.get("hosts")
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict) or values[0].get("runner_id") != runner_id or values[0].get("online") is not True or not isinstance(host_values, list) or not all(isinstance(value, dict) for value in host_values):
        return Rejected("runner_not_reconnected", "the exact sole old runner must be online")
    matching = [value for value in host_values if value.get("host_id", "").replace("-", "") == OLD_HOST_ID]
    if len(matching) != 1 or matching[0].get("status") != "online" or matching[0].get("gateway_inference") is None:
        return Rejected("host_not_reconnected", "old host must have a fresh in-memory handshake")
    return digest(_bytes({"info": info, "me": me, "header_me": header_me, "runners": runners, "hosts": hosts}))


def _database(uri: str, workspace: str) -> tuple[str, str] | Rejected:
    """Read one coherent SQLite snapshot; only heartbeat time is volatile."""
    path = Path(uri.removeprefix("sqlite:///"))
    if not path.is_absolute() or path.resolve(strict=True) != path:
        return Rejected("unsupported_database", "one canonical local SQLite database is required")
    def cell(value: object) -> object:
        return {"blob": value.hex()} if isinstance(value, bytes) else value
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        records: dict[str, object] = {}
        raw: dict[str, list[dict[str, object]]] = {}
        for table in ("conversations", "omnigent_conversation_metadata", "conversation_labels", "conversation_items"):
            cursor = connection.execute(f'SELECT * FROM "{table}"')
            columns = [description[0] for description in cursor.description]
            rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
            raw[table] = rows
            records[table] = sorted(({key: cell(value) for key, value in row.items() if key != "runner_last_seen"} for row in rows), key=canonical)
        old_id = bytes.fromhex(OLD_SESSION_ID)
        old = [row for row in raw["omnigent_conversation_metadata"] if row["id"] == old_id]
        old_items = [row for row in raw["conversation_items"] if row["conversation_id"] == old_id]
        if len(raw["conversations"]) != 4 or len(old) != 1 or len(old_items) != 22:
            return Rejected("session_inventory_mismatch", "refresh requires exactly four sessions and 22 old transcript items")
        value = old[0]
        if value.get("host_id") != bytes.fromhex(OLD_HOST_ID) or value.get("external_session_id") != OLD_THREAD_ID or value.get("workspace") != workspace or not isinstance(value.get("runner_id"), str):
            return Rejected("old_binding_mismatch", "stored old host/thread/workspace/runner binding differs")
        return digest(_bytes(records)), str(value["runner_id"])


async def _thread(socket_path: str, old_native_cwd: str) -> str | Rejected:
    from omnigent.codex_native_app_server import CodexAppServerClient

    client = CodexAppServerClient(ws_url=socket_path, client_name="omo-server-refresh-readonly")
    try:
        await client.connect()
        response = await client.request("thread/read", {"threadId": OLD_THREAD_ID, "includeTurns": True})
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict):
            return Rejected("native_thread_unobservable", "thread/read did not return a thread object")
        turns = thread.get("turns")
        if thread.get("cwd") != old_native_cwd:
            return Rejected("native_cwd_mismatch", "old native cwd differs from the explicit predecessor binding")
        if thread.get("id") != OLD_THREAD_ID or not isinstance(turns, list) or len(turns) != 3 or any(not isinstance(turn, dict) or turn.get("status") != "completed" for turn in turns):
            return Rejected("native_not_idle", "old native thread must match and contain exactly three completed turns")
        return digest(_bytes(thread))
    finally:
        await client.close()


def observe(request: RefreshRequest, database_uri: str) -> PreservedState | Rejected:
    try:
        config = read_config(Path(request.backend_config_path))
        if isinstance(config, Rejected):
            return config
        if config.old_session_id != OLD_SESSION_ID:
            return Rejected("old_binding_mismatch", "backend does not name the authorized predecessor")
        bridge_bytes = Path(config.bridge_state_path).read_bytes()
        bridge = json.loads(bridge_bytes)
        if not isinstance(bridge, dict) or bridge.get("session_id", "").replace("-", "") != OLD_SESSION_ID or bridge.get("thread_id") != OLD_THREAD_ID or bridge.get("active_turn_id") is not None or not isinstance(bridge.get("socket_path"), str):
            return Rejected("native_not_idle", "bridge must bind the exact idle predecessor")
        if "cwd" not in bridge or bridge["cwd"] != request.old_bridge_cwd:
            return Rejected("bridge_cwd_mismatch", "old bridge cwd differs from the explicit nullable predecessor binding")
        domain = process_domain(config.host_pid, (OLD_THREAD_ID, config.bridge_state_path, bridge["socket_path"]))
        if isinstance(domain, Rejected):
            return domain
        database = _database(database_uri, request.workspace)
        if isinstance(database, Rejected):
            return database
        thread = asyncio.run(asyncio.wait_for(_thread(bridge["socket_path"], request.old_native_cwd), timeout=15))
        if isinstance(thread, Rejected):
            return thread
        public = _public(config.server_url, database[1])
        if isinstance(public, Rejected):
            return public
        if Path(config.bridge_state_path).read_bytes() != bridge_bytes:
            return Rejected("bridge_drift", "native bridge changed during observation")
        later = process_domain(config.host_pid, (OLD_THREAD_ID, config.bridge_state_path, bridge["socket_path"]))
        if isinstance(later, Rejected):
            return later
        if tuple(value.pin for value in later) != tuple(value.pin for value in domain):
            return Rejected("host_process_drift", "native process census changed during observation")
        return PreservedState(tuple(value.pin for value in domain), database[0], thread, digest(bridge_bytes), public, database[1])
    except (OSError, ValueError, RuntimeError, TimeoutError, sqlite3.Error, urllib.error.URLError) as exc:
        return Rejected("refresh_unobservable", str(exc))


def _server_environment(pid: int) -> tuple[dict[bytes, bytes], str]:
    """Keep credentials only in memory; bind initial exec environment bytes."""
    data = (Path("/proc") / str(pid) / "environ").read_bytes()
    entries = [entry.partition(b"=") for entry in data.split(b"\0") if entry]
    values = {key: value for key, separator, value in entries if separator and key}
    if len(values) != len(entries):
        raise ValueError("server environment has malformed or duplicate names")
    return values, digest(data)


def _argv_hash(argv: tuple[str, ...]) -> str:
    return digest(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")


def _command(request: RefreshRequest, old: Process) -> tuple[str, ...]:
    return (old.argv[0], "-B", "-m", "omo_manager.omo_omnigent_server", *old.argv[4:], "--fence-database", request.fence_database,
            "--replacement-socket", request.control_socket, "--replacement-preparation", request.preparation_path,
            "--replacement-packet", request.packet_path, "--replacement-review", request.approval_path,
            "--replacement-backend-config", request.backend_config_path, "--replacement-preparation-sha256", digest(Path(request.preparation_path).read_bytes()))


def _scope(request: RefreshRequest) -> Rejected | None:
    import yaml

    try:
        workspace = Path(request.workspace)
        if Path(request.task_path).name != TASK_NAME or workspace.name != WORKSPACE_NAME or workspace.resolve(strict=True) != workspace or not workspace.is_dir() or workspace.stat().st_uid != os.geteuid() or workspace != configured_workspace():
            return Rejected("wrong_refresh_scope", "only the existing Source1957 task and workspace are supported")
        if not isinstance(request.old_native_cwd, str) or request.old_bridge_cwd is not None and not isinstance(request.old_bridge_cwd, str):
            return Rejected("invalid_predecessor_cwd", "native cwd must be explicit text and bridge cwd explicit text or null")
        for value in (request.old_native_cwd, request.old_bridge_cwd):
            if value is not None and (not Path(value).is_absolute() or Path(value).resolve(strict=True) != Path(value) or not Path(value).is_dir()):
                return Rejected("invalid_predecessor_cwd", "predecessor cwd bindings must be canonical existing directories")
        artifacts = (request.journal_path, request.log_path, request.fence_database, request.control_socket, request.packet_path, request.approval_path, request.preparation_path, request.backend_config_path)
        if len(set(artifacts)) != len(artifacts):
            return Rejected("refresh_path_alias", "all private refresh/control artifacts must be distinct")
        config = yaml.safe_load(Path(request.global_config_path).read_bytes())
        host = config.get("host") if isinstance(config, dict) else None
        if Path(request.global_config_path).name != "config.yaml" or not isinstance(config, dict) or set(config) != {"host"} or not isinstance(host, dict) or host.get("host_id") != OLD_HOST_ID:
            return Rejected("unsupported_global_config", "refresh supports the observed host-only global configuration")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return Rejected("unsupported_refresh_scope", str(exc))
    return None


def prepare(request: RefreshRequest) -> RefreshPlan | Rejected:
    """Observe only; even the plan file is published separately by the CLI."""
    try:
        invalid = _published(request)
        if invalid:
            return invalid
        invalid = _scope(request)
        if invalid:
            return invalid
        for name in ("journal_path", "log_path", "fence_database", "control_socket", "packet_path", "approval_path"):
            path = Path(getattr(request, name))
            invalid = _private(path, required=False)
            if invalid:
                return invalid
            if path.exists():
                return Rejected("refresh_output_exists", f"{name} must not already exist")
        for name in ("preparation_path", "backend_config_path"):
            invalid = _private(Path(getattr(request, name)), required=True)
            if invalid:
                return invalid
        old = process(request.server_pid, "server")
        if not isinstance(old, Process):
            return old if isinstance(old, Rejected) else Rejected("server_absent", "original server is absent")
        storage = _old_command(old)
        if isinstance(storage, Rejected):
            return storage
        if not _listens(old.pin.pid, storage[2]):
            return Rejected("server_socket_mismatch", "original PID does not exclusively own the loopback listener")
        package = validate_package()
        if isinstance(package, BootstrapRejected):
            return Rejected(package.code, package.detail)
        config = read_config(Path(request.backend_config_path))
        if isinstance(config, Rejected):
            return config
        if config.server_url != f"http://127.0.0.1:{storage[2]}":
            return Rejected("server_url_mismatch", "backend URL must match the authenticated original listener")
        paths = (request.authority_path, request.task_path, request.todo_path, request.global_config_path, request.preparation_path, request.backend_config_path, config.host_config_path, str(configuration_source()))
        pins = tuple(_snapshot(path) for path in paths)
        if pins[0].sha256 != AUTHORITY_SHA256:
            return Rejected("authority_mismatch", "Source1957 bytes differ")
        task = Path(request.task_path).read_bytes()
        parsed = metadata(task, Path(request.task_path).parent, "refresh task")
        if parsed.runat != f"omnigent://{OLD_SESSION_ID}" or len(parsed.pending_task_items) != 9:
            return Rejected("queue_mismatch", "the old owner and unchanged nine-item queue are required")
        argv = _command(request, old)
        original_environment, environment_hash = _server_environment(old.pin.pid)
        original_config_home = original_environment.get(b"OMNIGENT_CONFIG_HOME")
        original_home = original_environment.get(b"HOME")
        config_directory = Path(os.fsdecode(original_config_home)) if original_config_home else Path(os.fsdecode(original_home or b"")) / ".omnigent"
        if config_directory / "config.yaml" != Path(request.global_config_path):
            return Rejected("original_config_mismatch", "the original environment must resolve the bound global configuration")
        original_manager_source = original_environment.get(b"OMO_MANAGER_LOCAL_ENV")
        manager_source = Path(os.fsdecode(original_manager_source)) if original_manager_source else Path(os.fsdecode(original_home or b"")) / ".config" / "omo_manager" / "local.env"
        if manager_source != configuration_source() or original_environment.get(b"OMO_SOURCE1957_WORKSPACE", os.fsencode(request.workspace)) != os.fsencode(request.workspace):
            return Rejected("original_config_mismatch", "the unchanged original environment must select the pinned manager workspace configuration")
        checked = subprocess.run((*argv, "--check"), cwd=request.checkout, env={os.fsdecode(key): os.fsdecode(value) for key, value in original_environment.items()}, capture_output=True, timeout=30)
        if checked.returncode:
            return Rejected("bootstrap_check_failed", "published bootstrap rejected its exact startup configuration")
        before = observe(request, storage[0])
        if isinstance(before, Rejected):
            return before
        if old.pin.pid in {value.pid for value in before.processes}:
            return Rejected("server_is_host", "server refresh cannot target any preserved host/native process")
        executable = os.readlink(f"/proc/{old.pin.pid}/exe")
        if executable != str(Path(old.argv[0]).resolve(strict=True)):
            return Rejected("executable_mismatch", "server executable differs from the supplied interpreter")
        return RefreshPlan(request, old.pin, executable, digest(Path(executable).read_bytes()), old.argv, argv, pins, _source_hash(Path(request.checkout)), package, digest(raw_queue(task)), before, environment_hash)
    except (OSError, ValueError, ReplaceError, subprocess.SubprocessError) as exc:
        return Rejected("refresh_prepare_failed", str(exc))


def check(plan: RefreshPlan, *, running_pid: int | None = None) -> Rejected | None:
    """Recheck all immutable inputs and independently observe the preserved owner."""
    try:
        invalid = _published(plan.request)
        if invalid:
            return invalid
        invalid = _scope(plan.request)
        if invalid:
            return invalid
        original = Process(plan.old_server, 0, os.geteuid(), "S", plan.old_argv)
        config = read_config(Path(plan.request.backend_config_path))
        if isinstance(config, Rejected):
            return config
        storage = _old_command(original)
        expected_paths = (plan.request.authority_path, plan.request.task_path, plan.request.todo_path, plan.request.global_config_path, plan.request.preparation_path, plan.request.backend_config_path, config.host_config_path, str(configuration_source()))
        if isinstance(storage, Rejected) or plan.old_server.pid != plan.request.server_pid or plan.old_server.writer_domain != "server" or _argv_hash(plan.old_argv) != plan.old_server.argv_sha256 or tuple(pin.path for pin in plan.files) != expected_paths or not plan.files or plan.files[0].sha256 != AUTHORITY_SHA256 or plan.new_argv != _command(plan.request, original) or config.server_url != f"http://127.0.0.1:{plan.old_argv[7]}" or plan.old_server.pid in {value.pid for value in plan.before.processes}:
            return Rejected("invalid_refresh_binding", "plan does not bind the exact supported incident and bootstrap")
        if tuple(_snapshot(pin.path) for pin in plan.files) != plan.files or _source_hash(Path(plan.request.checkout)) != plan.sources_sha256 or digest(Path(plan.executable).read_bytes()) != plan.interpreter_sha256:
            return Rejected("refresh_input_drift", "reviewed files, published sources or interpreter changed")
        package = validate_package()
        if package != plan.package_sha256 or package != OMNIGENT_SOURCE_SHA256:
            return Rejected("package_drift", "installed package no longer matches the plan")
        task = Path(plan.request.task_path).read_bytes()
        parsed = metadata(task, Path(plan.request.task_path).parent, "refresh task")
        if parsed.runat != f"omnigent://{OLD_SESSION_ID}" or len(parsed.pending_task_items) != 9 or digest(raw_queue(task)) != plan.queue_sha256:
            return Rejected("queue_mismatch", "refresh must preserve the exact reviewed old task and nine-item queue")
        old = process(plan.old_server.pid, "server")
        if running_pid is None:
            if not same_process(old, plan.old_server) or os.readlink(f"/proc/{plan.old_server.pid}/exe") != plan.executable:
                return Rejected("server_process_drift", "the exact original server incarnation is absent or changed")
        elif isinstance(old, Rejected) or isinstance(old, Process) and old.pin.start_ticks == plan.old_server.start_ticks and old.state != "Z":
            return Rejected("old_server_not_gone", "positive old-server exit is required")
        if not _listens(running_pid or plan.old_server.pid, int(plan.old_argv[7])):
            return Rejected("server_socket_mismatch", "the expected server does not exclusively own its listener")
        if _server_environment(running_pid or plan.old_server.pid)[1] != plan.old_environment_sha256:
            return Rejected("server_environment_drift", "server environment differs from the original private in-memory custody")
        observed = observe(plan.request, plan.old_argv[9])
        if isinstance(observed, Rejected):
            return observed
        if observed != plan.before:
            return Rejected("preserved_state_drift", "host/native identities, sessions, items, native history or effective auth changed")
        return None
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("refresh_check_failed", str(exc))


def _load(path: Path, expected_sha256: str) -> RefreshPlan | Rejected:
    invalid = _private(path, required=True)
    if invalid:
        return invalid
    try:
        snapshot = read_snapshot(path, "refresh plan")
        plan = TypeAdapter(RefreshPlan).validate_json(snapshot.data)
        if digest(snapshot.data) != expected_sha256 or _bytes(asdict(plan)) != snapshot.data:
            return Rejected("refresh_plan_mismatch", "plan must be canonical and match its reviewed digest")
        return plan
    except (OSError, ValueError, ValidationError, ReplaceError) as exc:
        return Rejected("invalid_refresh_plan", str(exc))


def _review(path: Path, expected_sha256: str, plan_sha256: str) -> Rejected | None:
    invalid = _private(path, required=True)
    if invalid:
        return invalid
    try:
        snapshot = read_snapshot(path, "refresh review")
        if digest(snapshot.data) != expected_sha256 or snapshot.data != f"approve Source1957 server refresh {plan_sha256}\n".encode():
            return Rejected("refresh_review_mismatch", "a separate exact-plan refresh approval is required")
    except ReplaceError as exc:
        return Rejected("refresh_review_mismatch", str(exc))
    return None


def _journal(path: Path) -> tuple[Snapshot, Journal]:
    invalid = _private(path, required=True)
    if invalid:
        raise ReplaceError(invalid.detail)
    snapshot = read_snapshot(path, "refresh journal")
    record = TypeAdapter(Journal).validate_json(snapshot.data)
    if _bytes(asdict(record)) != snapshot.data:
        raise ReplaceError("refresh journal is not canonical")
    return snapshot, record


def _record(snapshot: Snapshot, journal: Journal, **changes: object) -> tuple[Snapshot, Journal]:
    updated = replace(journal, **changes)
    return replace_snapshot(snapshot, _bytes(asdict(updated)), "refresh journal"), updated


# 🧑 "Replace the current read-only owner atomically ... preserving the task and queue and delivering only open work."
def standby(plan_path: Path, plan_sha256: str, review_path: Path, review_sha256: str) -> Rejected | None:
    """Persist this detached standby's identity before the only allowed signal.

    The standby owns SIGTERM, positive pidfd exit, and exec. Supervisor death
    cannot strand an unlaunched successor after stopping the original server.
    It never retries a launch and never escalates to SIGKILL or host signals.
    """
    plan = _load(plan_path, plan_sha256)
    if isinstance(plan, Rejected):
        return plan
    invalid = _review(review_path, review_sha256, plan_sha256)
    if invalid:
        return invalid
    try:
        with task_file_lock(Path("/proc") / str(plan.old_server.pid) / "stat"), task_file_lock(Path(plan.request.task_path)), task_file_lock(Path(plan.request.todo_path)):
            return _handover(plan, plan_sha256, review_sha256)
    except (OSError, ValueError) as exc:
        return Rejected("refresh_lock_failed", str(exc))


def _handover(plan: RefreshPlan, plan_sha256: str, review_sha256: str) -> Rejected | None:
    journal_path = Path(plan.request.journal_path)
    handle: int | None = None
    try:
        with task_file_lock(journal_path):
            snapshot, journal = _journal(journal_path)
            if (journal.plan_sha256, journal.review_sha256, journal.phase, journal.child) != (plan_sha256, review_sha256, "launch_intent", None):
                return Rejected("refresh_already_claimed", "only one recorded standby may own this handover")
            invalid = check(plan)
            if invalid:
                _record(snapshot, journal, phase="rejected", detail=invalid.code)
                return invalid
            handle = pidfd_open(plan.old_server.pid)
            if not same_process(process(plan.old_server.pid, "server"), plan.old_server):
                return Rejected("server_process_drift", "original server changed before pidfd binding")
            invalid = check(plan)
            if invalid:
                _record(snapshot, journal, phase="rejected", detail=invalid.code)
                return invalid
            original_environment, environment_hash = _server_environment(plan.old_server.pid)
            if environment_hash != plan.old_environment_sha256:
                return Rejected("server_environment_drift", "original environment changed before custody transfer")
            child = process(os.getpid(), "refresh_standby")
            if not isinstance(child, Process):
                return Rejected("standby_unobservable", "cannot record the already-running standby")
            snapshot, journal = _record(snapshot, journal, phase="standby", child=child.pin)
            snapshot, journal = _record(snapshot, journal, phase="stopping")
            pidfd_signal(handle, signal.SIGTERM)
        select.select([handle], [], [])
        os.close(handle)
        handle = None
        with task_file_lock(journal_path):
            snapshot, journal = _journal(journal_path)
            if (journal.plan_sha256, journal.review_sha256, journal.phase, journal.child) != (plan_sha256, review_sha256, "stopping", child.pin):
                return Rejected("refresh_journal_drift", "standby lost its exact durable launch claim")
            snapshot, journal = _record(snapshot, journal, phase="starting")
        os.chdir(plan.request.checkout)
        os.execve(plan.new_argv[0], plan.new_argv, original_environment)
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("refresh_standby_failed", str(exc))
    finally:
        if handle is not None:
            os.close(handle)


def _child_matches(plan: RefreshPlan, pin: ProcessPin) -> bool:
    value = process(pin.pid, pin.writer_domain)
    expected_argv = _argv_hash(plan.new_argv)
    return isinstance(value, Process) and value.state != "Z" and value.pin.start_ticks == pin.start_ticks and value.pin.boot_id == pin.boot_id and value.pin.argv_sha256 == expected_argv and os.readlink(f"/proc/{pin.pid}/exe") == plan.executable


def reconcile(plan_path: Path, plan_sha256: str, review_path: Path, review_sha256: str, *, timeout_s: float = 90) -> Journal | Rejected:
    """Observe only; an uncertain launch intent never creates another child."""
    plan = _load(plan_path, plan_sha256)
    if isinstance(plan, Rejected):
        return plan
    invalid = _review(review_path, review_sha256, plan_sha256)
    if invalid:
        return invalid
    path = Path(plan.request.journal_path)
    deadline = time.monotonic() + timeout_s
    last = Rejected("refresh_pending", "standby identity/startup is not yet proven; no retry was launched")
    try:
        while True:
            with task_file_lock(path):
                snapshot, journal = _journal(path)
                if (journal.plan_sha256, journal.review_sha256) != (plan_sha256, review_sha256):
                    return Rejected("refresh_journal_mismatch", "journal belongs to another reviewed plan")
                if journal.phase == "rejected":
                    return Rejected("refresh_rejected", journal.detail)
                if journal.child is not None and _child_matches(plan, journal.child):
                    invalid = check(plan, running_pid=journal.child.pid)
                    if invalid is None:
                        if journal.phase != "verified":
                            snapshot, journal = _record(snapshot, journal, phase="verified")
                        return journal
                    last = invalid
                elif journal.child is not None:
                    child = process(journal.child.pid, journal.child.writer_domain)
                    if not same_process(child, journal.child):
                        return Rejected("refresh_child_uncertain", "recorded standby is absent or changed; never launch again automatically")
            if time.monotonic() >= deadline:
                return last
            time.sleep(0.2)
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("refresh_reconcile_failed", str(exc))


def execute(plan_path: Path, plan_sha256: str, review_path: Path, review_sha256: str, *, timeout_s: float = 90) -> Journal | Rejected:
    plan = _load(plan_path, plan_sha256)
    if isinstance(plan, Rejected):
        return plan
    invalid = _review(review_path, review_sha256, plan_sha256)
    if invalid:
        return invalid
    path = Path(plan.request.journal_path)
    try:
        with task_file_lock(path):
            if not path.exists():
                invalid = check(plan)
                if invalid:
                    return invalid
                original_environment, environment_hash = _server_environment(plan.old_server.pid)
                if environment_hash != plan.old_environment_sha256:
                    return Rejected("server_environment_drift", "original environment changed before standby launch")
                invalid = _private(path, required=False) or _private(Path(plan.request.log_path), required=False)
                if invalid:
                    return invalid
                log_fd = os.open(plan.request.log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                try:
                    create_snapshot(path, _bytes(asdict(Journal(plan_sha256, review_sha256, "launch_intent"))), 0o600)
                    args = (plan.old_argv[0], "-B", "-m", "omo_manager.omo_omnigent_refresh", "_standby", "--plan", str(plan_path), "--plan-sha256", plan_sha256, "--review", str(review_path), "--review-sha256", review_sha256)
                    child = subprocess.Popen(args, cwd=plan.request.checkout, env={os.fsdecode(key): os.fsdecode(value) for key, value in original_environment.items()}, stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd, start_new_session=True, close_fds=True)
                    Thread(target=child.wait, name="refresh-child-reaper", daemon=True).start()
                finally:
                    os.close(log_fd)
        return reconcile(plan_path, plan_sha256, review_path, review_sha256, timeout_s=timeout_s)
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("refresh_execute_uncertain", str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check", "execute", "reconcile", "_standby"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", default="")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--review-sha256", default="")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            if args.request is None:
                parser.error("prepare requires --request")
            invalid = _private(args.request, required=True) or _private(args.plan, required=False)
            if invalid:
                result: object = invalid
            else:
                request = TypeAdapter(RefreshRequest).validate_json(read_snapshot(args.request, "refresh request").data)
                result = prepare(request)
                if isinstance(result, RefreshPlan):
                    published = create_snapshot(args.plan, _bytes(asdict(result)), 0o600)
                    result = {"plan_sha256": digest(published.data), "old_server_pid": result.old_server.pid}
        elif args.action == "check":
            plan = _load(args.plan, args.plan_sha256)
            result = plan if isinstance(plan, Rejected) else check(plan)
        else:
            if args.review is None:
                parser.error("execution/reconciliation requires --review")
            action = {"execute": execute, "reconcile": reconcile, "_standby": standby}[args.action]
            result = action(args.plan, args.plan_sha256, args.review, args.review_sha256)
    except (OSError, ValueError, ReplaceError) as exc:
        result = Rejected("refresh_command_failed", str(exc))
    print(result)
    return 2 if isinstance(result, Rejected) else 0


if __name__ == "__main__":
    raise SystemExit(main())
