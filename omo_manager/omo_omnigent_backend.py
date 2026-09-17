"""Trusted, process-bound execution behind the local replacement control socket."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Callable, Coroutine
from typing import final
from urllib.parse import urlsplit

from fastapi import FastAPI

from omo_manager.omo_omnigent import FULL_ACCESS_LABEL
from omo_manager.omo_omnigent_fence import FenceStore, Operation, Rejected, canonical, digest
from omo_manager.omo_omnigent_replace import ExactPacket, ProcessPin, RuntimeState, decoded, encoded

@dataclass(frozen=True)
class Process:
    pin: ProcessPin
    parent_pid: int
    uid: int
    state: str
    argv: tuple[str, ...]


def pidfd_open(pid: int) -> int:
    """Use Linux's stable syscall ABI when portable Python omits pidfd wrappers."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise OSError("reviewed pidfd ABI requires Linux x86_64 or aarch64")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def pidfd_signal(descriptor: int, number: int) -> None:
    """Signal only the incarnation represented by a previously opened pidfd."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(424), ctypes.c_int(descriptor), ctypes.c_int(number), ctypes.c_void_p(), ctypes.c_uint(0))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def process(pid: int, domain: str) -> Process | Rejected | None:
    """Read a process incarnation without following a reused numeric PID."""
    directory = Path("/proc") / str(pid)
    try:
        before = directory.joinpath("stat").read_text()
        fields = before[before.rindex(")") + 2:].split()
        argv_bytes = directory.joinpath("cmdline").read_bytes()
        uid = directory.stat().st_uid
        after = directory.joinpath("stat").read_text()
        later = after[after.rindex(")") + 2:].split()
        if (fields[1], fields[19]) != (later[1], later[19]):
            return Rejected("process_drift", f"process {pid} changed during inspection")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        pin = ProcessPin(pid, int(fields[19]), boot, digest(argv_bytes), domain)
        return Process(pin, int(fields[1]), uid, fields[0], tuple(os.fsdecode(arg) for arg in argv_bytes.split(b"\0") if arg))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, IndexError) as exc:
        return Rejected("process_unobservable", f"process {pid}: {exc}")


def process_domain(host_pid: int, identifiers: tuple[str, ...]) -> tuple[Process, ...] | Rejected:
    """Find the complete same-user descendant tree and reject outside writers.

    The host is a Linux child subreaper; detached native terminals remain in
    its descendant tree. A matching native thread, bridge or socket outside
    that tree is an independently controlled writer, never a cleanup target.
    """
    values: dict[int, Process] = {}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        return Rejected("process_unobservable", str(exc))
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.geteuid():
                continue
        except FileNotFoundError:
            continue
        observed = process(int(entry.name), "host")
        if isinstance(observed, Rejected):
            return observed
        if observed is not None and observed.state != "Z":
            values[observed.pin.pid] = observed
    root = values.get(host_pid)
    if root is None:
        return Rejected("host_process_absent", str(host_pid))
    owned = {host_pid}
    while children := {pid for pid, value in values.items() if value.parent_pid in owned} - owned:
        owned.update(children)
    for pid, value in values.items():
        if pid not in owned and any(identifier in argument for identifier in identifiers for argument in value.argv):
            return Rejected("external_native_writer", f"process {pid} references the old native owner outside the bound host")
    return tuple(values[pid] for pid in sorted(owned))


def same_process(actual: Process | Rejected | None, expected: ProcessPin) -> bool:
    return isinstance(actual, Process) and actual.pin == expected and actual.state != "Z"


def old_processes_gone(pins: tuple[ProcessPin, ...]) -> Rejected | None:
    for pin in pins:
        observed = process(pin.pid, pin.writer_domain)
        if isinstance(observed, Rejected):
            return observed
        if same_process(observed, pin):
            return Rejected("native_process_alive", f"bound process {pin.pid} remains live")
        if isinstance(observed, Process) and observed.pin.start_ticks == pin.start_ticks and observed.state != "Z":
            return Rejected("native_process_changed", f"bound process {pin.pid} changed argv before exiting")
    return None


def local_server_port(value: object) -> int | Rejected:
    try:
        if not isinstance(value, str):
            return Rejected("invalid_server_url", "server URL must be text")
        parsed = urlsplit(value)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.port is None or parsed.port < 1:
            return Rejected("invalid_server_url", "server must be one explicit loopback HTTP origin without credentials")
        return parsed.port
    except ValueError as exc:
        return Rejected("invalid_server_url", str(exc))


def verify_host_socket(pin: ProcessPin, peer: tuple[str, int], server_port: int) -> Rejected | None:
    """Bind the registered WebSocket's TCP peer to the exact child process.

    The same persisted host ID can be used by two daemons. Linux's live socket
    inode ownership, not that ID or a heartbeat, distinguishes their tunnels.
    The helper is the spawned host's parent and must be able to inspect its
    descriptors; unavailable ownership evidence fails closed.
    """
    if peer[0] != "127.0.0.1" or not same_process(process(pin.pid, pin.writer_domain), pin):
        return Rejected("host_socket_identity", "host process or local transport peer changed")
    try:
        owned: set[str] = set()
        for descriptor in (Path("/proc") / str(pin.pid) / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                owned.add(target[8:-1])
        rows = (Path("/proc") / str(pin.pid) / "net/tcp").read_text().splitlines()[1:]
        local, remote = f"0100007F:{peer[1]:04X}", f"0100007F:{server_port:04X}"
        found = any(len(parts := row.split()) > 9 and parts[1] == local and parts[2] == remote and parts[3] == "01" and parts[9] in owned for row in rows)
    except OSError as exc:
        return Rejected("host_socket_unobservable", str(exc))
    if not found or not same_process(process(pin.pid, pin.writer_domain), pin):
        return Rejected("foreign_host_connection", "registered host tunnel is not owned by the operation's exact host process")
    return None


# 🧑 "Replace the current read-only owner atomically with one unrestricted OmniGent session ... preserving the task and queue and delivering only open work."
def stop_domain(pins: tuple[ProcessPin, ...], host_pid: int, identifiers: tuple[str, ...], timeout_s: float = 10) -> Rejected | None:
    """Freeze, recheck, then kill only pinned incarnations through Linux pidfds.

    Freezing closes fork races before the destructive boundary. Any pre-kill
    mismatch resumes only processes frozen by this invocation. A crash leaves
    the durable server fence intact; no new owner can dispatch without a
    separate positive old-domain disappearance proof.
    """
    if not pins or host_pid not in {pin.pid for pin in pins}:
        return Rejected("invalid_process_domain", "host process is missing from the reviewed domain")
    observed = process_domain(host_pid, identifiers)
    if isinstance(observed, Rejected):
        return observed
    if tuple(value.pin for value in observed) != pins:
        return Rejected("process_drift", "host descendant census differs from the reviewed packet")
    if any(value.state in {"T", "t"} for value in observed):
        return Rejected("process_already_stopped", "a paused or traced process needs separate custody, not an automatic resume")
    handles: dict[int, int] = {}
    frozen: list[int] = []
    destructive = False
    try:
        for pin in pins:
            handles[pin.pid] = pidfd_open(pin.pid)
            current = process(pin.pid, pin.writer_domain)
            if not same_process(current, pin):
                return Rejected("process_drift", f"process {pin.pid} changed before pidfd binding")
        order = [host_pid, *[pin.pid for pin in pins if pin.pid != host_pid]]
        for pid in order:
            pidfd_signal(handles[pid], signal.SIGSTOP)
            frozen.append(pid)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            current = [process(pin.pid, pin.writer_domain) for pin in pins]
            if all(isinstance(value, Process) and value.state in {"T", "t"} for value in current):
                break
            time.sleep(0.01)
        else:
            return Rejected("freeze_timeout", "not every pinned process entered a stopped state")
        frozen_domain = process_domain(host_pid, identifiers)
        if isinstance(frozen_domain, Rejected):
            return frozen_domain
        if tuple(value.pin for value in frozen_domain) != pins:
            return Rejected("process_drift", "host spawned or lost a process during the freeze barrier")
        destructive = True
        for pid in reversed(order):
            pidfd_signal(handles[pid], signal.SIGKILL)
        deadline = time.monotonic() + timeout_s
        for descriptor in handles.values():
            if not select.select([descriptor], [], [], max(0, deadline - time.monotonic()))[0]:
                return Rejected("process_exit_unconfirmed", "a pinned process did not confirm exit")
        return old_processes_gone(pins)
    except (OSError, ValueError) as exc:
        return Rejected("process_barrier_failed", str(exc))
    finally:
        if not destructive:
            for pid in reversed(frozen):
                try:
                    pidfd_signal(handles[pid], signal.SIGCONT)
                except ProcessLookupError:
                    pass
        for descriptor in handles.values():
            os.close(descriptor)


@dataclass(frozen=True)
class BackendConfig:
    old_session_id: str
    host_pid: int
    bridge_state_path: str
    host_config_path: str
    server_url: str


def read_config(path: Path) -> BackendConfig | Rejected:
    try:
        info = path.lstat()
        if path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return Rejected("unsafe_backend_config", "backend configuration must be canonical, owner-only and same-user")
        value = json.loads(path.read_bytes())
        config = BackendConfig(**value)
        if type(config.host_pid) is not int or config.host_pid <= 1 or isinstance(local_server_port(config.server_url), Rejected):
            return Rejected("invalid_backend_config", "the reviewed backend supports one explicit local host")
        return config
    except (OSError, TypeError, ValueError) as exc:
        return Rejected("invalid_backend_config", str(exc))


def _json_bytes(value: object) -> bytes:
    return canonical(value).encode()


def _object(value: object) -> dict[str, object]:
    return {key: member for key, member in value.items() if isinstance(key, str)} if isinstance(value, dict) else {}


def _receipt(packet: ExactPacket, **values: object) -> dict[str, object]:
    receipt = {"operation_id": packet.operation_id, **values}
    return {**receipt, "evidence_sha256": digest(_json_bytes(receipt))}


async def verify_empty_native_thread(ws_url: str, thread_id: str, workspace: str) -> dict[str, object] | Rejected:
    """Inspect and no-override resume only a fresh, provably empty thread.

    Codex 0.153.4 has no settings-read operation. Its resume response reports
    actual sandbox settings; an empty new thread contains no turn to replay.
    Never apply this operation to the predecessor or a thread with history.
    """
    from omnigent.codex_native_app_server import CodexAppServerClient

    client = CodexAppServerClient(ws_url=ws_url, client_name="omo-replacement-verifier")
    try:
        await client.connect()
        before = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = _object(_object(before.get("result")).get("thread"))
        if thread.get("id") != thread_id or thread.get("turns") != [] or thread.get("cwd") != workspace:
            return Rejected("new_native_history_mismatch", "fresh thread must have exact workspace and zero turns before settings inspection")
        response = await client.request("thread/resume", {"threadId": thread_id})
        settings = _object(response.get("result"))
        resumed = _object(settings.get("thread"))
        if resumed.get("id") != thread_id or resumed.get("turns") != [] or settings.get("cwd") != workspace or settings.get("sandbox") != {"type": "dangerFullAccess"} or settings.get("approvalPolicy") != "never" or settings.get("model") != "gpt-6-astra" or settings.get("reasoningEffort") != "ultra":
            return Rejected("new_native_settings_mismatch", "new native thread does not report the exact unrestricted workspace, model and effort")
        after = await client.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = _object(_object(after.get("result")).get("thread"))
        if thread.get("id") != thread_id or thread.get("turns") != [] or thread.get("cwd") != workspace:
            return Rejected("new_native_history_drift", "fresh thread changed during settings inspection")
        return {"thread_id": thread_id, "workspace": workspace, "settings_sha256": digest(_json_bytes(settings)), "before_sha256": digest(_json_bytes(before)), "after_sha256": digest(_json_bytes(after))}
    except (OSError, RuntimeError, ValueError) as exc:
        return Rejected("native_settings_unobservable", str(exc))
    finally:
        await client.close()


@dataclass(frozen=True)
class BackendResources:
    inner_app: FastAPI
    fence_store: FenceStore
    database_uri: str
    conversation_database_uri: str | None
    artifact_location: str
    validate_package: Callable[[], str | Rejected]


@final
class OmnigentBackend:
    """The private control server's concrete backend; never import into a client.

    Raw store access is confined to operation-bound creation and inspection.
    Actual runner traffic still traverses the guarded server transports. All
    asynchronous registry access runs on the original server event loop.
    """

    def __init__(self, resources: BackendResources, loop: asyncio.AbstractEventLoop, config_path: Path, config: BackendConfig) -> None:
        from omnigent.runner.routing import RunnerRouter
        from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
        from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

        self.application = resources
        self.loop = loop
        self.config_path = config_path
        self.config = config
        self.host_child: subprocess.Popen[bytes] | None = None
        self.store = SqlAlchemyConversationStore(resources.database_uri, resources.conversation_database_uri)
        self.permissions = SqlAlchemyPermissionStore(resources.database_uri)
        state = resources.inner_app.state
        self.hosts = state.host_registry
        self.runners = state.tunnel_registry
        self.router = RunnerRouter(registry=self.runners, conversation_store=self.store, host_registry=self.hosts, host_store=state.host_store)

    def _run[T](self, coroutine: Coroutine[object, object, T], timeout_s: float = 90) -> T:
        async def bounded() -> T:
            async with asyncio.timeout(timeout_s):
                return await coroutine

        return asyncio.run_coroutine_threadsafe(bounded(), self.loop).result()

    def _files(self) -> dict[str, str] | Rejected:
        try:
            return {str(path): digest(path.read_bytes()) for path in (self.config_path, Path(self.config.bridge_state_path), Path(self.config.host_config_path))}
        except OSError as exc:
            return Rejected("runtime_file_unobservable", str(exc))

    def _bridge(self) -> dict[str, object] | Rejected:
        try:
            value = json.loads(Path(self.config.bridge_state_path).read_bytes())
            if not isinstance(value, dict):
                return Rejected("invalid_bridge", "native bridge state is not an object")
            return value
        except (OSError, ValueError) as exc:
            return Rejected("invalid_bridge", str(exc))

    def _agent(self, agent_id: str) -> dict[str, object] | Rejected:
        from omnigent.stores.artifact_store.local import LocalArtifactStore

        agent = self.application.inner_app.state.agent_store.get(agent_id)
        if agent is None or agent.name != "codex-native-ui" or agent.session_id is not None:
            return Rejected("unsupported_agent", "replacement requires the existing shared native Codex wrapper")
        try:
            bundle = LocalArtifactStore(self.application.artifact_location).get(agent.bundle_location)
        except (OSError, KeyError, ValueError) as exc:
            return Rejected("agent_bundle_unobservable", str(exc))
        return {"metadata": asdict(agent), "bundle_sha256": digest(bundle)}

    async def _registry(self, host_id: str) -> dict[str, object] | Rejected:
        connection = self.hosts.get(host_id)
        if connection is None:
            return Rejected("host_offline", host_id)
        pending = sorted(connection.pending_launches)
        return {
            "host_id": host_id, "owner": connection.owner, "hello": asdict(connection.hello),
            "connected_at": connection.connected_at, "pending_launch_ids": pending,
            "online_runner_ids": sorted(self.runners.online_runner_ids()),
        }

    def observe(self) -> RuntimeState | Rejected:
        conversation = self.store.get_conversation(self.config.old_session_id)
        if conversation is None or conversation.runner_id is None or conversation.host_id is None or conversation.external_session_id is None or conversation.agent_id is None or conversation.workspace is None:
            return Rejected("missing_owner_binding", "old session lacks the exact runner, host or native thread")
        fence = self.application.fence_store
        generation = fence.generation(conversation.id), fence.host_generation(conversation.host_id)
        registry = self._run(self._registry(conversation.host_id))
        files = self._files()
        if isinstance(registry, Rejected):
            return registry
        online = registry.get("online_runner_ids")
        pending = registry.get("pending_launch_ids")
        if not isinstance(online, list) or not isinstance(pending, list) or any(not isinstance(value, str) for value in [*online, *pending]):
            return Rejected("invalid_registry", "registry runner and pending-launch identifiers must be strings")
        if isinstance(files, Rejected):
            return files
        identifiers = (conversation.external_session_id, str(Path(self.config.bridge_state_path).parent))
        domain = process_domain(self.config.host_pid, identifiers)
        if isinstance(domain, Rejected):
            return domain
        items = self.store.list_items(conversation.id, limit=1000)
        if items.has_more:
            return Rejected("item_census_truncated", "old session exceeds the reviewed complete item census")
        bridge = self._bridge()
        if isinstance(bridge, Rejected):
            return bridge
        agent = self._agent(conversation.agent_id)
        if isinstance(agent, Rejected):
            return agent
        routing = {"files": files, "bridge": bridge, "backend_config": asdict(self.config), "agent": agent}
        if generation != (fence.generation(conversation.id), fence.host_generation(conversation.host_id)):
            return Rejected("routing_drift", "routing changed during runtime inspection")
        return RuntimeState(
            conversation.id, conversation.runner_id, conversation.host_id, conversation.external_session_id,
            encoded(_json_bytes(asdict(conversation))), encoded(_json_bytes([item.to_api_dict() for item in items.data])),
            encoded(_json_bytes(registry)), encoded(_json_bytes(routing)), tuple(value.pin for value in domain),
            tuple(value for value in online if isinstance(value, str)), tuple(value for value in pending if isinstance(value, str)), *generation, conversation.workspace,
        )

    def ready(self, packet: ExactPacket) -> Rejected | None:
        package = self.application.validate_package()
        if isinstance(package, Rejected) or package != packet.package_sha256:
            return Rejected("package_drift", "installed OmniGent differs from the reviewed packet")
        if packet.runtime.session_id != self.config.old_session_id:
            return Rejected("wrong_backend_owner", "packet belongs to a different configured owner")
        if packet.runtime.online_runner_ids != (packet.runtime.runner_id,) or packet.runtime.pending_launch_ids:
            return Rejected("host_not_exclusive", "cold barrier requires the sole old runner and no queued host launch")
        files = self._files()
        if isinstance(files, Rejected):
            return files
        pins = {source.path: source.sha256 for source in packet.sources}
        if any(pins.get(path) != sha for path, sha in files.items()):
            return Rejected("unbound_runtime_files", "packet must pin backend configuration, host identity and bridge state")
        host = process(self.config.host_pid, "host")
        expected_argv = (sys.executable, "-m", "omnigent.host._daemon_entry", "--server", self.config.server_url)
        if not isinstance(host, Process) or host.argv != expected_argv:
            return Rejected("unsupported_host_command", "host daemon command differs from the supported explicit local launch")
        from omnigent.host.identity import CONFIG_PATH, HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, load_host_identity_if_present

        if Path(self.config.host_config_path) != CONFIG_PATH.resolve() or any(os.environ.get(key) for key in (HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR)):
            return Rejected("host_config_override", "supported restart must use the pinned default host identity without environment overrides")
        identity = load_host_identity_if_present(Path(self.config.host_config_path))
        if identity is None or identity.host_id != packet.runtime.host_id:
            return Rejected("host_identity_drift", "existing host identity does not match the packet")
        bridge = self._bridge()
        if isinstance(bridge, Rejected):
            return bridge
        if bridge.get("session_id") != packet.runtime.session_id or bridge.get("thread_id") != packet.runtime.thread_id or bridge.get("active_turn_id") is not None:
            return Rejected("native_thread_drift", "bridge must identify the exact idle old native thread")
        listener = bridge.get("socket_path")
        if not isinstance(listener, str) or not listener.startswith("ws://127.0.0.1:"):
            return Rejected("unsupported_native_listener", "native listener must be an explicit loopback WebSocket")
        domain = process_domain(self.config.host_pid, (packet.runtime.thread_id, listener, str(Path(self.config.bridge_state_path).parent)))
        if isinstance(domain, Rejected):
            return domain
        app_servers = [value for value in domain if Path(value.argv[0]).name == "codex" and "app-server" in value.argv and listener in value.argv]
        if len(app_servers) != 1 or not any("--remote" in value.argv and listener in value.argv and packet.runtime.thread_id in value.argv for value in domain):
            return Rejected("native_process_binding", "one native app-server and its exact remote thread terminal must belong to the host")
        try:
            descriptor = pidfd_open(os.getpid())
            os.close(descriptor)
        except OSError as exc:
            return Rejected("pidfd_unavailable", str(exc))
        return None

    def _operation(self, packet: ExactPacket, phases: set[str]) -> Rejected | None:
        operation = self.application.fence_store.get(packet.operation_id)
        if operation is None or operation.spec != packet.spec or operation.phase not in phases or operation.status == "cancelled":
            return Rejected("operation_mismatch", "operation, packet or lifecycle phase differs")
        return None

    def resolve_native_pin(self, operation: Operation) -> dict[str, object] | Rejected:
        """Authenticate the new native writer before its metadata PATCH exists."""
        from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, read_bridge_state

        current = self.application.fence_store.get(operation.spec.operation_id)
        if current is None or current.spec != operation.spec or current.status == "cancelled" or current.phase not in {"committed", "delivery_confirmed"}:
            return Rejected("native_operation_mismatch", "native ingress requires the current committed replacement")
        host_receipt = current.receipts.get("host_restarted", {}).get("process")
        launch = current.receipts.get("successor_launch", {})
        if not isinstance(host_receipt, dict) or not isinstance(launch.get("runner_id"), str):
            return Rejected("native_launch_unbound", "native ingress has no exact host and claimed runner")
        host_pin = ProcessPin(**host_receipt)
        if not same_process(process(host_pin.pid, host_pin.writer_domain), host_pin):
            return Rejected("native_host_drift", "restarted host incarnation is no longer live")
        connection = self.hosts.get(current.spec.expected.host_id)
        if invalid := self._host_connection_pin(connection, host_pin):
            return invalid
        session_id = current.spec.new_session_id
        successor = self.store.get_conversation(session_id)
        bridge_dir = bridge_dir_for_bridge_id(session_id)
        bridge = read_bridge_state(bridge_dir)
        if successor is None or successor.runner_id != launch["runner_id"] or successor.host_id != current.spec.expected.host_id or successor.workspace != current.spec.expected.workspace or bridge is None or bridge.session_id != session_id or bridge.thread_id == current.spec.expected.thread_id or successor.external_session_id not in {None, bridge.thread_id}:
            return Rejected("native_binding_drift", "bridge, claimed runner and reserved session disagree")
        if not bridge.socket_path.startswith("ws://127.0.0.1:"):
            return Rejected("native_listener_mismatch", "native ingress requires the private loopback listener")
        domain = process_domain(host_pin.pid, (bridge.thread_id, bridge.socket_path, str(bridge_dir)))
        if isinstance(domain, Rejected):
            return domain
        native = [value for value in domain if value.argv and Path(value.argv[0]).name == "codex" and "app-server" in value.argv and bridge.socket_path in value.argv]
        if len(native) != 1:
            return Rejected("native_writer_ambiguous", "exactly one native app-server must belong to the operation-owned host")
        owner = self.store.get_session_owner(session_id)
        if owner != "local":
            return Rejected("native_owner_mismatch", "reserved successor does not have the authenticated local owner")
        pin = native[0].pin
        return {"session_id": session_id, "runner_id": successor.runner_id, "owner": owner, "thread_id": bridge.thread_id, "pid": pin.pid, "start_ticks": pin.start_ticks, "boot_id": pin.boot_id, "argv_sha256": pin.argv_sha256, "host_pid": host_pin.pid, "host_start_ticks": host_pin.start_ticks, "host_id": successor.host_id}

    def _host_connection_pin(self, connection: object, pin: ProcessPin) -> Rejected | None:
        ws = getattr(connection, "ws", None)
        scope = getattr(ws, "scope", {})
        peer = scope.get("client") if isinstance(scope, dict) else None
        port = local_server_port(self.config.server_url)
        if isinstance(port, Rejected):
            return port
        if not isinstance(peer, (tuple, list)) or len(peer) != 2 or not isinstance(peer[0], str) or type(peer[1]) is not int:
            return Rejected("host_peer_unobservable", "host connection lacks an exact TCP peer")
        return verify_host_socket(pin, (peer[0], peer[1]), port)

    async def _drained(self, packet: ExactPacket, timeout_s: float = 10) -> Rejected | None:
        deadline = self.loop.time() + timeout_s
        while self.loop.time() < deadline:
            if self.hosts.get(packet.runtime.host_id) is None and self.runners.get(packet.runtime.runner_id) is None:
                return None
            await asyncio.sleep(0.02)
        return Rejected("transport_not_drained", "old host or runner transport remains connected after native process exit")

    def quiesce(self, packet: ExactPacket) -> dict[str, object] | Rejected:
        if invalid := self._operation(packet, {"fenced"}):
            return invalid
        current = self.observe()
        if isinstance(current, Rejected):
            return current
        if current != packet.runtime:
            return Rejected("runtime_drift", "runtime changed after the guarded fence")
        identifiers = (packet.runtime.thread_id, str(Path(self.config.bridge_state_path).parent))
        if invalid := stop_domain(packet.runtime.processes, self.config.host_pid, identifiers):
            return invalid
        return self.inspect_receipt(packet, "old_quiesced")

    def prepare_successor(self, packet: ExactPacket) -> dict[str, object] | Rejected:
        if invalid := self._operation(packet, {"old_quiesced"}):
            return invalid
        if invalid := old_processes_gone(packet.runtime.processes):
            return invalid
        old = self.store.get_conversation(packet.runtime.session_id)
        if old is None or old.agent_id is None or not old.title:
            return Rejected("old_agent_missing", "the existing Codex agent definition and nonempty task title are required")
        if self._agent(old.agent_id) != json.loads(decoded(packet.runtime.routing_b64))["agent"]:
            return Rejected("agent_drift", "reviewed native agent bundle or definition changed")
        if self.store.get_conversation(packet.successor_id) is not None:
            return Rejected("successor_exists", "reserved successor already exists; inspect, never create twice")
        creation_title = f"omo-replacement:{packet.operation_id}:{secrets.token_hex(32)}"
        intent = self.application.fence_store.record_evidence(packet.operation_id, "successor_create_intent", _receipt(packet, new_session_id=packet.successor_id, agent_id=old.agent_id, title=old.title, creation_title=creation_title, host_id=packet.runtime.host_id, workspace=packet.workspace.path))
        if isinstance(intent, Rejected):
            return intent
        created = self.store.create_conversation(conversation_id=packet.successor_id, agent_id=old.agent_id, host_id=packet.runtime.host_id, workspace=packet.workspace.path, title=creation_title)
        recorded = self.application.fence_store.record_evidence(packet.operation_id, "successor_created", _receipt(packet, conversation=asdict(created)))
        if isinstance(recorded, Rejected):
            return recorded
        return self.reconcile_effect(packet, "new_prepared")

    def reconcile_effect(self, packet: ExactPacket, phase: str) -> dict[str, object] | Rejected:
        from omnigent.entities import Conversation

        if phase == "delivery_confirmed":
            if invalid := self._operation(packet, {"committed"}):
                return invalid
            observed = self.inspect_receipt(packet, phase)
            if not isinstance(observed, Rejected) or observed.code != "owned_delivery_pre_dispatch":
                return observed
            return self.deliver(packet)
        if phase != "new_prepared":
            return Rejected("unsupported_reconciliation", phase)
        if invalid := self._operation(packet, {"old_quiesced"}):
            return invalid
        operation = self.application.fence_store.get(packet.operation_id)
        assert operation is not None
        recorded = operation.receipts.get("successor_created", {}).get("conversation")
        intent = operation.receipts.get("successor_create_intent", {})
        successor = self.store.get_conversation(packet.successor_id)
        if not isinstance(recorded, dict):
            if successor is not None and successor.title == intent.get("creation_title") and successor.host_id is None and successor.workspace is None:
                intended_agent = intent.get("agent_id")
                if not isinstance(intended_agent, str):
                    return Rejected("invalid_creation_intent", "original agent binding is missing")
                baseline = Conversation(id=packet.successor_id, created_at=successor.created_at, updated_at=successor.updated_at, title=successor.title, root_conversation_id=packet.successor_id, agent_id=intended_agent)
                if asdict(successor) != asdict(baseline) or self.store.list_items(packet.successor_id, limit=1).data or self.store.list_child_conversation_ids_by_parent([packet.successor_id]).get(packet.successor_id):
                    return Rejected("partial_creation_drift", "the nonce-owned AP-only row has acquired state")
                rollback = self.application.fence_store.record_evidence(packet.operation_id, "partial_creation_rollback", _receipt(packet, conversation=asdict(successor)))
                if isinstance(rollback, Rejected):
                    return rollback
                if not self._run(self.store.delete_conversation(packet.successor_id)):
                    return Rejected("partial_creation_rollback_unknown", "the exact nonce-owned staged row disappeared before rollback")
                successor = None
            if successor is None:
                refreshed = self.application.fence_store.get(packet.operation_id)
                if refreshed is None or "partial_creation_rollback" not in refreshed.receipts:
                    return Rejected("creation_outcome_unknown", "no owned row is observable; never retry an unproved creation")
                agent_id, creation_title = intent.get("agent_id"), intent.get("creation_title")
                if not isinstance(agent_id, str) or not isinstance(creation_title, str):
                    return Rejected("invalid_creation_intent", "rollback lacks exact original creation values")
                successor = self.store.create_conversation(conversation_id=packet.successor_id, agent_id=agent_id, host_id=packet.runtime.host_id, workspace=packet.workspace.path, title=creation_title)
            if successor.title != intent.get("creation_title") or successor.agent_id != intent.get("agent_id") or successor.root_conversation_id != packet.successor_id or successor.parent_conversation_id is not None or successor.runner_id is not None or successor.external_session_id is not None or successor.labels or successor.workspace != packet.workspace.path or successor.host_id != packet.runtime.host_id or self.store.list_items(packet.successor_id, limit=1).data:
                return Rejected("creation_outcome_unknown", "same-ID row lacks the exact atomic creation nonce and empty bound state")
            record = self.application.fence_store.record_evidence(packet.operation_id, "successor_created", _receipt(packet, conversation=asdict(successor)))
            if isinstance(record, Rejected):
                return record
            recorded = asdict(successor)
        if successor is None:
            return Rejected("creation_outcome_unknown", "durably created successor disappeared")
        actual = asdict(successor)
        for key, value in recorded.items():
            if key not in {"updated_at", "model_override", "reasoning_effort", "labels", "title"} and actual.get(key) != value:
                return Rejected("successor_creation_drift", f"owned successor field {key} changed")
        if successor.title not in {intent.get("creation_title"), intent.get("title")}:
            return Rejected("successor_creation_drift", "owned successor title changed")
        items = self.store.list_items(packet.successor_id, limit=1)
        if items.data or items.has_more or successor.runner_id is not None or successor.external_session_id is not None:
            return Rejected("successor_creation_drift", "owned successor has been launched or received work")
        if successor.model_override not in {None, "gpt-6-astra"} or successor.reasoning_effort not in {None, "ultra"}:
            return Rejected("successor_creation_drift", "owned successor model settings changed")
        old = self.store.get_conversation(packet.runtime.session_id)
        if old is None or old.agent_id is None or self._agent(old.agent_id) != json.loads(decoded(packet.runtime.routing_b64))["agent"]:
            return Rejected("agent_drift", "reviewed native agent definition changed")
        labels = {key: value for key, value in old.labels.items() if key in {"omnigent.ui", "omnigent.wrapper", "omnigent.codex_native.collaboration_mode"}}
        labels = {**labels, FULL_ACCESS_LABEL: "1", "omo.replacement.operation": packet.operation_id}
        if any(labels.get(key) != value for key, value in successor.labels.items()):
            return Rejected("successor_creation_drift", "owned successor labels changed")
        title = intent.get("title")
        if title is not None and not isinstance(title, str):
            return Rejected("invalid_creation_intent", "creation title is not text")
        self.store.update_conversation(packet.successor_id, title=title, model_override="gpt-6-astra", reasoning_effort="ultra")
        self.store.set_labels(packet.successor_id, labels)
        from omnigent.server.auth import LEVEL_OWNER

        self.permissions.ensure_user("local")
        self.permissions.grant("local", packet.successor_id, LEVEL_OWNER)
        return self.inspect_receipt(packet, "new_prepared")

    def _restart_host(self, packet: ExactPacket) -> Rejected | None:
        operation = self.application.fence_store.get(packet.operation_id)
        if operation is None:
            return Rejected("operation_mismatch", packet.operation_id)
        if invalid := old_processes_gone(packet.runtime.processes):
            return invalid
        files = self._files()
        original = json.loads(decoded(packet.runtime.routing_b64))["files"]
        if isinstance(files, Rejected):
            return files
        if files != original:
            return Rejected("restart_config_drift", "host configuration or bridge state changed after quiescence")
        argv = [sys.executable, "-m", "omnigent.cli", "host", "--server", self.config.server_url, "--non-interactive"]
        if "host_restart_intent" in operation.receipts:
            receipt = operation.receipts.get("host_restarted", operation.receipts.get("host_restart_standby", {}))
            saved = receipt.get("process")
            if not isinstance(saved, dict) or receipt.get("argv") != argv:
                return Rejected("host_restart_unknown", "restart has no durably identified standby incarnation; never create another")
            saved_pin = ProcessPin(**saved)
            running = process(saved_pin.pid, saved_pin.writer_domain)
            if not isinstance(running, Process) or (running.pin.start_ticks, running.pin.boot_id) != (saved_pin.start_ticks, saved_pin.boot_id) or running.argv != tuple(argv):
                return Rejected("host_restart_unknown", "recorded standby has not become its exact host command; no blind restart or release")
            connection = self.hosts.get(packet.runtime.host_id)
            if invalid := self._host_connection_pin(connection, running.pin):
                return invalid
            if "host_restarted" not in operation.receipts:
                recorded = self.application.fence_store.record_evidence(packet.operation_id, "host_restarted", _receipt(packet, process=asdict(running.pin), argv=argv))
                return recorded if isinstance(recorded, Rejected) else None
            return None
        if self._run(self._host_connected(packet.runtime.host_id)):
            return Rejected("unexpected_host_reconnect", "a host connected before the operation-owned restart")
        recorded = self.application.fence_store.record_evidence(packet.operation_id, "host_restart_intent", _receipt(packet, argv=argv, old_host_pid=self.config.host_pid))
        if isinstance(recorded, Rejected):
            return recorded
        if "host_restarted" in recorded.receipts:
            return Rejected("host_restart_already_issued", "inspect the owned incarnation; never start a second host")
        release_token = secrets.token_hex(32)
        standby_argv = [sys.executable, "-m", "omo_manager.omo_omnigent_backend", "--host-standby", self.config.server_url, release_token]
        try:
            self.host_child = subprocess.Popen(standby_argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            return Rejected("host_restart_failed", str(exc))
        child = process(self.host_child.pid, "replacement-host")
        if not isinstance(child, Process) or child.argv != tuple(standby_argv):
            if self.host_child.stdin is not None:
                self.host_child.stdin.close()
            return Rejected("host_restart_identity_unknown", "standby child did not retain its exact command; no release was sent")
        result = self.application.fence_store.record_evidence(packet.operation_id, "host_restart_standby", _receipt(packet, process=asdict(child.pin), argv=argv, standby_argv_sha256=child.pin.argv_sha256))
        if isinstance(result, Rejected):
            if self.host_child.stdin is not None:
                self.host_child.stdin.close()
            return result
        assert self.host_child.stdin is not None
        try:
            self.host_child.stdin.write(f"{release_token}\n".encode())
            self.host_child.stdin.close()
        except OSError as exc:
            return Rejected("host_release_unknown", str(exc))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            running = process(child.pin.pid, "replacement-host")
            if isinstance(running, Process) and running.pin.start_ticks == child.pin.start_ticks and running.argv == tuple(argv):
                result = self.application.fence_store.record_evidence(packet.operation_id, "host_restarted", _receipt(packet, process=asdict(running.pin), argv=argv))
                return result if isinstance(result, Rejected) else None
            if self.host_child.poll() is not None:
                return Rejected("host_restart_failed", f"standby exited {self.host_child.returncode}")
            time.sleep(0.02)
        return Rejected("host_restart_unconfirmed", "standby incarnation did not enter its bound host command")

    async def _host_connected(self, host_id: str) -> bool:
        return self.hosts.get(host_id) is not None

    def _bridge_gate(self, packet: ExactPacket, *, first_launch: bool) -> Rejected | None:
        from omnigent.codex_native_bridge import CODEX_NATIVE_BRIDGE_ID_LABEL_KEY, bridge_dir_for_bridge_id
        from omnigent.runner.turn_routing import MARKER_FILE, PENDING_FILE

        bridge = bridge_dir_for_bridge_id(packet.successor_id)
        if bridge.resolve() != bridge or any(path.is_symlink() for path in (bridge, *bridge.parents)):
            return Rejected("successor_bridge_alias", "reserved bridge path has a symlink or noncanonical ancestor")
        sessions = self.store.list_conversations(limit=1000, kind=None, include_archived=True)
        if sessions.has_more:
            return Rejected("bridge_census_truncated", "cannot prove the reserved bridge has no session alias")
        for session in sessions.data:
            bridge_id = session.labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY, session.id)
            if bridge_dir_for_bridge_id(bridge_id) == bridge and (session.id != packet.successor_id or bridge_id != packet.successor_id):
                return Rejected("successor_bridge_alias", "another stored session aliases the reserved native bridge")
        if first_launch and bridge.exists():
            return Rejected("successor_bridge_exists", "fresh successor bridge must be absent before any launch")
        if any(os.path.lexists(bridge / name) for name in (PENDING_FILE, MARKER_FILE)):
            return Rejected("successor_replay_artifact", "pending native replay or its routing marker exists; initialization is forbidden")
        return None

    async def _launch_and_deliver(self, packet: ExactPacket) -> Rejected | None:
        import secrets

        from omnigent.host.frames import HostLaunchRunnerFrame, encode_host_frame
        from omnigent.runner.identity import token_bound_runner_id
        from omnigent.server.runner_session_init import RunnerSessionInitializer
        from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, read_bridge_state

        deadline = self.loop.time() + 30
        while self.hosts.get(packet.runtime.host_id) is None and self.loop.time() < deadline:
            await asyncio.sleep(0.05)
        connection = self.hosts.get(packet.runtime.host_id)
        if connection is None:
            return Rejected("host_restart_unconfirmed", "same host did not reconnect")
        operation = self.application.fence_store.get(packet.operation_id)
        assert operation is not None
        if "manifest_dispatch_intent" in operation.receipts:
            return Rejected("delivery_unknown", "one-shot manifest dispatch was already claimed; never resend")
        host_receipt = operation.receipts.get("host_restarted", {}).get("process")
        if not isinstance(host_receipt, dict):
            return Rejected("host_restart_identity_unknown", "new host has no durable process incarnation receipt")
        host_pin = ProcessPin(**host_receipt)
        if not same_process(process(host_pin.pid, host_pin.writer_domain), host_pin):
            return Rejected("host_restart_identity_drift", "operation-owned host incarnation is no longer live")
        if invalid := self._host_connection_pin(connection, host_pin):
            return invalid
        launch = operation.receipts.get("successor_launch")
        if isinstance(launch, dict):
            runner_id = launch.get("runner_id")
            successor = self.store.get_conversation(packet.successor_id)
            if not isinstance(runner_id, str) or successor is None or successor.runner_id != runner_id or set(self.runners.online_runner_ids()) - {runner_id} or set(connection.pending_launches) - {launch.get("request_id")}:
                return Rejected("claimed_runner_drift", "only the existing operation-claimed runner may be initialized after recovery")
        else:
            if invalid := self._bridge_gate(packet, first_launch=True):
                return invalid
            absence = self.application.fence_store.record_evidence(packet.operation_id, "native_bridge_absent", _receipt(packet, new_session_id=packet.successor_id, bridge_path=str(bridge_dir_for_bridge_id(packet.successor_id)), alias_absent=True, pending_replay_absent=True))
            if isinstance(absence, Rejected):
                return absence
            if self.runners.online_runner_ids() or connection.pending_launches:
                return Rejected("host_census_drift", "another runner or pending launch appeared before successor launch")
            token, request_id = secrets.token_urlsafe(32), secrets.token_hex(16)
            runner_id = token_bound_runner_id(token)
            if not self.store.set_runner_id(packet.successor_id, runner_id):
                return Rejected("successor_already_bound", "reserved successor already has an unclaimed runner; no duplicate launch")
            future = self.loop.create_future()
            connection.pending_launches[request_id] = future
            self.hosts.send_text(connection, encode_host_frame(HostLaunchRunnerFrame(request_id=request_id, binding_token=token, workspace=packet.workspace.path, session_id=packet.successor_id, harness="codex")))
            result = await asyncio.wait_for(asyncio.shield(future), 30)
            if result.get("status") != "launched" or result.get("runner_id") != runner_id:
                return Rejected("successor_launch_unconfirmed", str(result.get("error") or result.get("status")))
        if await self.runners.wait_for_runner(runner_id, timeout_s=30) is None:
            return Rejected("successor_connect_timeout", "claimed successor runner did not connect")
        successor = self.store.get_conversation(packet.successor_id)
        if successor is None:
            return Rejected("successor_missing", packet.successor_id)
        if invalid := self._bridge_gate(packet, first_launch=False):
            return invalid
        routed = self.router.client_for_conversation(conversation_id=packet.successor_id, harness="codex")
        initializer = RunnerSessionInitializer(self.runners, server_version="0.11.0")
        with self.application.fence_store.successor_context(packet.operation_id):
            response = await initializer.initialize(successor, routed.client, timeout=30, suppress_recovery_turn=True)
            if response.status_code >= 400:
                return Rejected("initialization_failed", f"runner initialization returned {response.status_code}")
            deadline = self.loop.time() + 30
            bridge = read_bridge_state(bridge_dir_for_bridge_id(packet.successor_id))
            while bridge is None and self.loop.time() < deadline:
                await asyncio.sleep(0.05)
                bridge = read_bridge_state(bridge_dir_for_bridge_id(packet.successor_id))
            successor = self.store.get_conversation(packet.successor_id)
            if bridge is None or successor is None or bridge.session_id != packet.successor_id or bridge.thread_id == packet.runtime.thread_id or bridge.active_turn_id is not None or successor.external_session_id != bridge.thread_id or not bridge.socket_path.startswith("ws://127.0.0.1:"):
                return Rejected("new_native_binding_mismatch", "new runner, bridge and native thread are not identically bound and idle")
            verification = await asyncio.wait_for(verify_empty_native_thread(bridge.socket_path, bridge.thread_id, packet.workspace.path), 30)
            if isinstance(verification, Rejected):
                return verification
            verified_key = "native_successor_verified" if "native_successor_verified" not in operation.receipts else f"native_successor_rechecked:{digest(_json_bytes(verification))}"
            native_receipt = self.application.fence_store.record_evidence(packet.operation_id, verified_key, _receipt(packet, **verification))
            if isinstance(native_receipt, Rejected):
                return native_receipt
            payload = {"role": "user", "content": [{"type": "input_text", "text": packet.prompt}]}
            claimed = self.application.fence_store.claim_manifest_dispatch(packet.operation_id, packet.delivery_id, digest(packet.prompt.encode()), runner_id, bridge.thread_id)
            if isinstance(claimed, Rejected):
                return claimed
            response = await routed.client.post(f"/v1/sessions/{packet.successor_id}/events", json=payload, timeout=30)
        if response.status_code >= 400:
            return Rejected("delivery_unknown", f"one-shot runner request returned {response.status_code}; never resend")
        return None

    def deliver(self, packet: ExactPacket) -> dict[str, object] | Rejected:
        import httpx

        from omnigent.errors import OmnigentError

        if invalid := self._operation(packet, {"committed"}):
            return invalid
        operation = self.application.fence_store.get(packet.operation_id)
        assert operation is not None
        if operation.receipts.get("delivery_intent", {}).get("delivery_id") != packet.delivery_id:
            return Rejected("delivery_not_claimed", "one-shot delivery must be durably claimed first")
        if "manifest_dispatch_intent" in operation.receipts:
            return self.inspect_receipt(packet, "delivery_confirmed")
        if invalid := self._bridge_gate(packet, first_launch="successor_launch" not in operation.receipts):
            return invalid
        old = self.store.get_conversation(packet.runtime.session_id)
        if old is None or old.agent_id is None or self._agent(old.agent_id) != json.loads(decoded(packet.runtime.routing_b64))["agent"]:
            return Rejected("agent_drift", "reviewed native agent definition changed before launch")
        if invalid := self._restart_host(packet):
            return invalid
        try:
            if invalid := self._run(self._launch_and_deliver(packet), 120):
                return invalid
        except (TimeoutError, OSError, RuntimeError, ValueError, httpx.HTTPError, OmnigentError) as exc:
            return Rejected("delivery_unknown", f"one-shot launch or delivery uncertain: {exc}")
        deadline = time.monotonic() + 30
        while True:
            receipt = self.inspect_receipt(packet, "delivery_confirmed")
            if not isinstance(receipt, Rejected) or receipt.code != "delivery_unknown" or time.monotonic() >= deadline:
                return receipt
            time.sleep(0.05)

    def inspect_receipt(self, packet: ExactPacket, phase: str) -> dict[str, object] | Rejected:
        if invalid := self._operation(packet, {"fenced", "old_quiesced", "new_prepared", "committed", "delivery_confirmed"}):
            return invalid
        if phase == "old_quiesced":
            if invalid := old_processes_gone(packet.runtime.processes):
                return invalid
            if invalid := self._run(self._drained(packet)):
                return invalid
            expected = packet.expected
            return _receipt(packet, session_id=expected.session_id, runner_id=expected.runner_id, thread_id=expected.thread_id, process_sha256=expected.process_sha256, host_sha256=expected.host_sha256, native_writers_gone=True, pending_launches_absent=True, transport_drained=True)
        successor = self.store.get_conversation(packet.successor_id)
        if phase == "new_prepared" and successor is not None:
            operation = self.application.fence_store.get(packet.operation_id)
            assert operation is not None
            owned_creation = "successor_created" in operation.receipts or successor.title == operation.receipts.get("successor_create_intent", {}).get("creation_title")
            if owned_creation and (successor.model_override != "gpt-6-astra" or successor.reasoning_effort != "ultra" or successor.labels.get("omo.replacement.operation") != packet.operation_id or successor.labels.get(FULL_ACCESS_LABEL) != "1" or self.store.get_session_owner(packet.successor_id) != "local"):
                return Rejected("owned_successor_incomplete", "durably identified successor initialization is incomplete; explicit reconciliation is required")
        if phase == "new_prepared" and successor is None:
            operation = self.application.fence_store.get(packet.operation_id)
            if operation is not None and "partial_creation_rollback" in operation.receipts:
                return Rejected("owned_successor_incomplete", "owned AP-only creation was rolled back; explicit reconciliation may recreate only its reserved ID")
        if successor is None or successor.workspace != packet.workspace.path or successor.host_id != packet.runtime.host_id or successor.labels.get("omo.replacement.operation") != packet.operation_id or successor.labels.get(FULL_ACCESS_LABEL) != "1" or successor.model_override != "gpt-6-astra" or successor.reasoning_effort != "ultra":
            return Rejected("successor_binding_mismatch", "reserved successor metadata is incomplete or changed")
        items = self.store.list_items(packet.successor_id, limit=1000)
        if items.has_more:
            return Rejected("successor_items_truncated", "cannot prove complete successor history")
        if phase == "new_prepared":
            if items.data or successor.runner_id is not None or successor.external_session_id is not None or successor.terminal_launch_args is not None or successor.parent_conversation_id is not None or self.store.list_child_conversation_ids_by_parent([packet.successor_id]).get(packet.successor_id):
                return Rejected("successor_not_empty", "successor is not unlaunched and empty")
            return _receipt(packet, new_session_id=packet.successor_id, workspace=successor.workspace, unrestricted=True, message_count=0, runner_absent=True)
        if phase == "delivery_confirmed":
            users = [item.to_api_dict() for item in items.data if item.type == "message" and item.to_api_dict().get("role") == "user"]
            operation = self.application.fence_store.get(packet.operation_id)
            assert operation is not None
            dispatch = operation.receipts.get("manifest_dispatch_intent")
            if not users and dispatch is None:
                return Rejected("owned_delivery_pre_dispatch", "no manifest dispatch was claimed; reviewed reconciliation may continue only the bound host and runner")
            if not isinstance(dispatch, dict) or dispatch.get("delivery_id") != packet.delivery_id or dispatch.get("prompt_sha256") != digest(packet.prompt.encode()):
                return Rejected("delivery_unknown", "native history has no matching one-shot manifest claim; never resend")
            if len(users) != 1 or users[0].get("content") != [{"type": "input_text", "text": packet.prompt}]:
                return Rejected("delivery_unknown", "exactly one reviewed open-work message is not yet durably observable; never resend")
            if successor.runner_id is None or not self.application.inner_app.state.runner_router.runner_is_online(successor.runner_id):
                return Rejected("successor_offline", "the sole successor runner is not online")
            return _receipt(packet, new_session_id=packet.successor_id, delivery_id=packet.delivery_id, prompt_sha256=digest(packet.prompt.encode()), item_id=users[0]["id"], runner_id=successor.runner_id)
        return Rejected("unknown_phase", phase)


def create_backend(resources: BackendResources, loop: asyncio.AbstractEventLoop, config_path: Path) -> OmnigentBackend | Rejected:
    config = read_config(config_path)
    if isinstance(config, Rejected):
        return config
    return OmnigentBackend(resources, loop, config_path, config)


def validate_backend_config(path: Path) -> Rejected | None:
    result = read_config(path)
    return result if isinstance(result, Rejected) else None


def host_standby(server_url: str, release_token: str) -> int:
    """Start no service until the parent durably records this incarnation.

    Parent death before release closes stdin, so an unrecorded child exits.
    Exec preserves PID and start ticks across the only service-start boundary.
    """
    if isinstance(local_server_port(server_url), Rejected) or len(release_token) != 64:
        return 2
    if sys.stdin.buffer.readline(128) != f"{release_token}\n".encode():
        return 2
    os.execv(sys.executable, [sys.executable, "-m", "omnigent.cli", "host", "--server", server_url, "--non-interactive"])
    return 2


if __name__ == "__main__":
    raise SystemExit(host_standby(sys.argv[2], sys.argv[3]) if len(sys.argv) == 4 and sys.argv[1] == "--host-standby" else 2)
