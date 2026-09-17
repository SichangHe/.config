#!/usr/bin/env python3
"""Source-bound OmniGent bootstrap and durable session admission at storage."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from itertools import chain
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, cast, final

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_omnigent_fence import OMNIGENT_SOURCE_SHA256, OMNIGENT_VERSION

if TYPE_CHECKING:
    from fastapi import FastAPI
    from omnigent.entities import Conversation
    from omnigent.spec.types import PolicySpec
    from omnigent.stores.conversation_store import ConversationStore, SessionConnectivity
    from starlette.types import ASGIApp
    from starlette.types import Message, Receive, Scope, Send

    from omo_manager.omo_omnigent_fence import Admission, FenceStore
    from omo_manager.omo_omnigent_ingress import NativeIngress
    from omo_manager.omo_omnigent_replace import ReplacementBackend

_READ_METHODS = frozenset({
    "get_session_owner",
    "list_child_conversation_ids_by_parent", "list_items", "list_latest_message_items_for_conversations",
    "search", "list_projects", "get_daily_cost", "sum_daily_cost",
    "list_daily_costs", "get_daily_cost_state",
})
_MUTATE_METHODS = frozenset({
    "append", "update_conversation", "rename_conversation_if_title_matches", "set_task_summary",
    "set_labels", "delete_label", "set_session_state", "set_session_usage", "set_conversation_project",
    "increment_session_usage", "set_runner_id", "set_session_live_status", "set_pending_elicitation_count",
    "replace_runner_id", "clear_runner_id", "clear_host_binding", "set_host_id", "set_external_session_id",
    "create_conversation", "create_session_with_agent", "fork_conversation", "switch_conversation_agent",
})
_GLOBAL_METHODS = frozenset({"add_daily_cost", "set_daily_ask_approved"})


@dataclass(frozen=True)
class BootstrapRejected:
    code: str
    detail: str


@dataclass(frozen=True)
class ReplacementControlOptions:
    socket_path: Path
    preparation_path: Path
    packet_path: Path
    approval_path: Path
    backend_config_path: Path
    preparation_sha256: str


@dataclass(frozen=True)
class ServerOptions:
    database_uri: str
    artifact_location: str
    fence_database: Path
    host: str = "127.0.0.1"
    port: int = 6767
    config_path: Path | None = None
    conversation_database_uri: str | None = None
    execution_timeout_s: int | None = None
    replacement_control: ReplacementControlOptions | None = None


@dataclass(frozen=True)
class ServerPlan:
    options: ServerOptions
    config: dict[str, object]
    execution_timeout_s: int
    source_sha256: str
    default_policies: list[PolicySpec]


@dataclass(frozen=True)
class ServerApplication:
    app: ASGIApp
    inner_app: FastAPI
    conversation_store: GuardedConversationStore
    fence_store: FenceStore
    plan: ServerPlan
    native_ingress: NativeIngress | None = None


def source_digest(package_root: Path) -> str:
    """Bind every installed Python source name and byte hash, excluding caches."""
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        digest.update(path.relative_to(package_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def validate_package() -> str | BootstrapRejected:
    """Reject incompatible installed code before importing its runtime or stores."""
    try:
        version = importlib.metadata.version("omnigent")
        spec = importlib.util.find_spec("omnigent")
        if version != OMNIGENT_VERSION:
            return BootstrapRejected("unsupported_version", "installed OmniGent version differs from the reviewed version")
        if spec is None or not spec.submodule_search_locations or len(spec.submodule_search_locations) != 1:
            return BootstrapRejected("unsupported_package", "OmniGent must resolve to one installed package directory")
        digest = source_digest(Path(spec.submodule_search_locations[0]))
    except (importlib.metadata.PackageNotFoundError, OSError):
        return BootstrapRejected("unavailable_package", "cannot verify the installed OmniGent source")
    if digest != OMNIGENT_SOURCE_SHA256:
        return BootstrapRejected("unsupported_source", "installed OmniGent source differs from the reviewed source")
    return digest


def _private_control_path(path: Path, *, required: bool, socket_file: bool = False) -> BootstrapRejected | None:
    try:
        parent = path.parent
        info = parent.stat()
        if not path.is_absolute() or parent.resolve(strict=True) != parent or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            return BootstrapRejected("invalid_control_path", "control paths require an existing canonical owned directory with mode 0700")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return BootstrapRejected("missing_control_file", "a required control file is absent") if required else None
        valid_kind = stat.S_ISSOCK(info.st_mode) if socket_file else stat.S_ISREG(info.st_mode)
        if not valid_kind or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            return BootstrapRejected("invalid_control_path", "control files must be owned, private, correctly typed and singly linked")
    except OSError:
        return BootstrapRejected("invalid_control_path", "cannot verify control path ownership")
    return None


def validate_control(options: ReplacementControlOptions) -> BootstrapRejected | None:
    paths = (options.socket_path, options.preparation_path, options.packet_path, options.approval_path, options.backend_config_path)
    if len(set(paths)) != len(paths) or re.fullmatch(r"[0-9a-f]{64}", options.preparation_sha256) is None:
        return BootstrapRejected("invalid_control_binding", "control paths must be distinct and preparation must have an exact SHA-256")
    if len(os.fsencode(options.socket_path)) >= 108:
        return BootstrapRejected("invalid_control_path", "control socket path exceeds the Linux Unix socket address limit")
    for path in paths:
        invalid = _private_control_path(path, required=path in (options.preparation_path, options.backend_config_path), socket_file=path == options.socket_path)
        if invalid:
            return invalid
    try:
        actual = hashlib.sha256(options.preparation_path.read_bytes()).hexdigest()
    except OSError:
        return BootstrapRejected("invalid_control_binding", "cannot read the pinned preparation")
    if actual != options.preparation_sha256:
        return BootstrapRejected("invalid_control_binding", "preparation differs from its startup commitment")
    return None


@dataclass(frozen=True)
class ControlRequest:
    action: str
    preparation_sha256: str
    packet_sha256: str
    approval_sha256: str


def _control_request(data: bytes) -> ControlRequest | BootstrapRejected:
    try:
        value: object = json.loads(data)
        if not isinstance(value, dict) or set(value) != {"action", "preparation_sha256", "packet_sha256", "approval_sha256"}:
            return BootstrapRejected("invalid_control_request", "only the exact Source1957 control envelope is accepted")
        if not all(isinstance(item, str) for item in value.values()):
            return BootstrapRejected("invalid_control_request", "control fields must be strings")
        if value["action"] not in {"prepare", "inspect", "execute", "reconcile"}:
            return BootstrapRejected("invalid_control_request", "unsupported control action")
        if any(item and re.fullmatch(r"[0-9a-f]{64}", item) is None for key, item in value.items() if key != "action"):
            return BootstrapRejected("invalid_control_request", "invalid control digest")
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
        if canonical != data:
            return BootstrapRejected("invalid_control_request", "control requests must use canonical JSON without duplicate fields")
        return ControlRequest(**value)
    except (ValueError, TypeError):
        return BootstrapRejected("invalid_control_request", "cannot decode the control envelope")


@dataclass(frozen=True)
class _ControlJob:
    request: ControlRequest
    result: asyncio.Future[dict[str, object]]


@final
class ReplacementControl:
    """One same-user Unix actor invokes the exact Source1957 coordinator.

    Callers supply only an action and content commitments. Paths, stores and
    the native backend are bound at server startup. Disconnecting the caller
    never cancels an accepted coordinator operation.
    """

    def __init__(self, options: ReplacementControlOptions, application: ServerApplication, backend: ReplacementBackend) -> None:
        self.options = options
        self.application = application
        self.backend = backend
        self._jobs: asyncio.Queue[_ControlJob | None] = asyncio.Queue(maxsize=8)
        self._worker: asyncio.Task[None] | None = None
        self._listener: asyncio.Server | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._accepting = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source1957-control")
        self._active_dispatch: Future[dict[str, object]] | None = None

    async def start(self) -> BootstrapRejected | None:
        invalid = validate_control(self.options)
        if invalid:
            return invalid
        path = self.options.socket_path
        if path.exists():
            previous = path.lstat()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    probe.connect(str(path))
                except ConnectionRefusedError:
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                        return BootstrapRejected("control_socket_changed", "control socket changed during recovery")
                    path.unlink()
                except OSError:
                    return BootstrapRejected("control_socket_unavailable", "cannot safely recover the previous control socket")
                else:
                    return BootstrapRejected("control_already_running", "a control listener already owns this socket")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            path.chmod(0o600)
            info = path.lstat()
            self._socket_identity = (info.st_dev, info.st_ino)
            listener.setblocking(False)
            self._listener = await asyncio.start_unix_server(self._receive, sock=listener, limit=8192)
            self._worker = asyncio.create_task(self._run())
            self._accepting = True
        except BaseException:
            listener.close()
            raise
        return None

    def stop_accepting(self) -> None:
        self._accepting = False
        if self._listener is not None:
            self._listener.close()

    async def close(self) -> None:
        self.stop_accepting()
        if self._listener is not None:
            await self._listener.wait_closed()
            self._listener = None
        try:
            if self._worker is not None:
                if not self._worker.done():
                    await self._jobs.put(None)
                await asyncio.shield(self._worker)
                self._worker = None
        finally:
            self._executor.shutdown(wait=self._active_dispatch is None or self._active_dispatch.done())
            try:
                info = self.options.socket_path.lstat()
                if (info.st_dev, info.st_ino) == self._socket_identity:
                    self.options.socket_path.unlink()
            except FileNotFoundError:
                pass

    async def _receive(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            peer_socket = writer.get_extra_info("socket")
            if peer_socket is None or not hasattr(socket, "SO_PEERCRED"):
                response = {"code": "unauthenticated_control", "detail": "Unix peer identity is unavailable"}
            else:
                _, uid, _ = struct.unpack("3i", peer_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                if uid != os.getuid():
                    response = {"code": "unauthenticated_control", "detail": "control peer must be the server owner"}
                else:
                    response = await self._request(reader)
            writer.write(json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, asyncio.TimeoutError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _request(self, reader: asyncio.StreamReader) -> dict[str, object]:
        data = await asyncio.wait_for(reader.readline(), timeout=5)
        if not data.endswith(b"\n"):
            return {"code": "invalid_control_request", "detail": "one terminated JSON line is required"}
        request = _control_request(data[:-1])
        if isinstance(request, BootstrapRejected):
            return asdict(request)
        if not self._accepting or self._worker is None or self._worker.done() or self._listener is None:
            return {"code": "control_unavailable", "detail": "control actor is not running"}
        future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
        try:
            self._jobs.put_nowait(_ControlJob(request, future))
        except asyncio.QueueFull:
            return {"code": "control_busy", "detail": "control queue is full"}
        return await asyncio.shield(future)

    async def _run(self) -> None:
        while (job := await self._jobs.get()) is not None:
            try:
                self._active_dispatch = self._executor.submit(self._dispatch, job.request)
                response = await asyncio.wrap_future(self._active_dispatch)
            except BaseException:
                self.stop_accepting()
                job.result.set_result({"code": "control_uncertain", "detail": "coordinator failed; inspect the durable operation before any retry"})
                while not self._jobs.empty():
                    pending = self._jobs.get_nowait()
                    if pending is not None:
                        pending.result.set_result({"code": "control_unavailable", "detail": "request was not dispatched because the coordinator stopped"})
                raise
            self._active_dispatch = None
            job.result.set_result(response)

    def _dispatch(self, request: ControlRequest) -> dict[str, object]:
        from omo_manager.omo_omnigent_fence import Rejected
        from omo_manager.omo_omnigent_replace import dispatch_control

        invalid = validate_control(self.options)
        if invalid:
            return asdict(invalid)
        if request.action == "prepare" and request.preparation_sha256 != self.options.preparation_sha256:
            return {"code": "preparation_mismatch", "detail": "preparation differs from the server startup commitment"}
        result = dispatch_control(
            request.action, self.options.preparation_path, self.options.packet_path, self.options.approval_path,
            request.preparation_sha256, request.packet_sha256, request.approval_sha256,
            self.application.fence_store, self.backend,
        )
        return asdict(result) if isinstance(result, Rejected) else result


# 🧑 "1" in reply to "Replace the current read-only owner atomically with one unrestricted OmniGent session ... preserving the task and queue and delivering only open work."
@final
class GuardedConversationStore:
    """Compose the pinned store; historical reads never authorize runner access.

    Every session write holds a durable admission until the underlying call ends.
    A denied store call is an invariant violation for callers behind the ASGI
    guard and raises `RuntimeError`; background writers fail before touching
    storage. No replacement bypass is exposed by this adapter.
    """

    def __init__(self, store: ConversationStore, fence: FenceStore) -> None:
        self._store = store
        self._fence = fence

    def _admit(self, session_ids: list[str], kind: str, host_id: str | None = None) -> Admission:
        from omo_manager.omo_omnigent_fence import Rejected

        result = self._fence.admit(session_ids, f"store:{kind}", host_id=host_id)
        if isinstance(result, Rejected):
            raise RuntimeError(f"session fenced: {result.code}")
        return result

    def _session_ids(self, arguments: dict[str, object]) -> list[str]:
        ids = [value for key, value in arguments.items() if key in {"conversation_id", "source_conversation_id", "parent_conversation_id"} and isinstance(value, str)]
        runner_id = arguments.get("runner_id")
        if isinstance(runner_id, str):
            session_id = self._fence.session_for_runner(runner_id)
            if session_id:
                ids.append(session_id)
        return ids

    def __getattr__(self, name: str) -> Callable[..., object]:
        if name in _READ_METHODS | _GLOBAL_METHODS:
            return cast(Callable[..., object], getattr(self._store, name))
        if name in {"find_imported_conversation", "list_conversations"}:
            method = getattr(self._store, name)

            def historical(*args: object, **kwargs: object) -> object:
                result = method(*args, **kwargs)
                if name == "find_imported_conversation":
                    return self._historical(result)
                return replace(result, data=[self._historical(conversation) for conversation in result.data])

            return historical
        if name not in _MUTATE_METHODS:
            raise AttributeError(f"unsupported conversation-store member: {name}")
        method = cast(Callable[..., object], getattr(self._store, name))

        def guarded(*args: object, **kwargs: object) -> object:
            arguments = inspect.signature(method).bind(*args, **kwargs).arguments
            host_id = arguments.get("host_id")
            admission = self._admit(self._session_ids(arguments), name, host_id if isinstance(host_id, str) else None)
            try:
                return method(*args, **kwargs)
            finally:
                self._fence.finish(admission)

        return guarded

    def _historical(self, conversation: Conversation | None) -> Conversation | None:
        if conversation is not None and self._fence.is_fenced(conversation.id):
            return replace(conversation, runner_id=None)
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._historical(self._store.get_conversation(conversation_id))

    def get_conversations(self, conversation_ids: list[str]) -> dict[str, Conversation | None]:
        conversations = self._store.get_conversations(conversation_ids)
        return {session_id: self._historical(conversation) for session_id, conversation in conversations.items()}

    async def delete_conversation(self, conversation_id: str) -> bool:
        subtree = {conversation_id}
        pending = [conversation_id]
        while pending:
            children = self._store.list_child_conversation_ids_by_parent(pending)
            pending = sorted(set(chain.from_iterable(children.values())) - subtree)
            subtree.update(pending)
        admission = self._admit(sorted(subtree), "delete_conversation")
        try:
            return await self._store.delete_conversation(conversation_id)
        finally:
            self._fence.finish(admission)

    def replacement_session(self, operation_id: str) -> Conversation | BootstrapRejected:
        """Return one operation-bound historical binding for trusted cleanup.

        This read grants no mutation or dispatch admission. The caller must
        authenticate its narrow coordinator endpoint before invoking it.
        """
        operation = self._fence.get(operation_id)
        if operation is None or operation.status == "cancelled":
            return BootstrapRejected("unknown_operation", "no active replacement is bound to this operation")
        expected = operation.spec.expected
        conversation = self._store.get_conversation(expected.session_id)
        if conversation is None or (
            conversation.runner_id, conversation.host_id, conversation.external_session_id, conversation.workspace
        ) != (expected.runner_id, expected.host_id, expected.thread_id, expected.workspace):
            return BootstrapRejected("binding_changed", "the stored owner differs from the replacement operation")
        return conversation

    def list_conversations_by_runner_id(self, runner_id: str) -> list[Conversation]:
        session_id = self._fence.session_for_runner(runner_id)
        if session_id and self._fence.is_fenced(session_id):
            return []
        conversations = self._store.list_conversations_by_runner_id(runner_id)
        return [conversation for conversation in conversations if not self._fence.is_fenced(conversation.id)]

    def get_runner_ids(self, conversation_ids: list[str]) -> dict[str, str | None]:
        runners = self._store.get_runner_ids(conversation_ids)
        return {session_id: None if self._fence.is_fenced(session_id) else runner_id for session_id, runner_id in runners.items()}

    def get_session_connectivity(self, conversation_ids: list[str]) -> dict[str, SessionConnectivity]:
        connectivity = self._store.get_session_connectivity(conversation_ids)
        return {
            session_id: replace(value, runner_id=None, runner_last_seen=None) if self._fence.is_fenced(session_id) else value
            for session_id, value in connectivity.items()
        }

    def touch_runner_liveness(self, runner_ids: list[str], now: int) -> None:
        for runner_id in runner_ids:
            self._runner_liveness("touch_runner_liveness", runner_id, now)

    def clear_runner_liveness(self, runner_id: str) -> None:
        self._runner_liveness("clear_runner_liveness", runner_id)

    def _runner_liveness(self, name: str, runner_id: str, now: int | None = None) -> None:
        from omo_manager.omo_omnigent_fence import Rejected

        ids = [conversation.id for conversation in self._store.list_conversations_by_runner_id(runner_id)]
        session_id = self._fence.session_for_runner(runner_id)
        if session_id:
            ids.append(session_id)
        admission = self._fence.admit(ids, f"store:{name}")
        if isinstance(admission, Rejected):
            return
        try:
            method = getattr(self._store, name)
            if now is None:
                method(runner_id)
            else:
                method([runner_id], now)
        finally:
            self._fence.finish(admission)


def validate_options(options: ServerOptions) -> ServerPlan | BootstrapRejected:
    """Validate the supported deployed configuration without lifecycle writes.

    This intentionally supports the observed loopback SQLite/local-artifact,
    header-auth deployment. It rejects other storage/auth/routing bootstraps
    rather than silently substituting defaults. Environment credentials are
    consumed by the upstream components and never returned or logged.
    """
    binding = validate_package()
    if isinstance(binding, BootstrapRejected):
        return binding
    from omo_manager.omo_omnigent_fence import validate_fence_path

    invalid_fence = validate_fence_path(options.fence_database)
    if invalid_fence:
        return BootstrapRejected(invalid_fence.code, invalid_fence.detail)
    if options.replacement_control is not None:
        invalid = validate_control(options.replacement_control)
        if invalid:
            return invalid
        from omo_manager.omo_omnigent_backend import validate_backend_config

        invalid_backend = validate_backend_config(options.replacement_control.backend_config_path)
        if invalid_backend:
            return BootstrapRejected(invalid_backend.code, invalid_backend.detail)
    import httpx
    import yaml
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    from starlette.requests import HTTPConnection

    from omnigent.errors import OmnigentError
    from omnigent.onboarding.provider_config import load_config as load_provider_config
    from omnigent.server.auth import UnifiedAuthProvider, local_single_user_enabled, resolve_auth_source

    if options.host != "127.0.0.1" or type(options.port) is not int or not 1 <= options.port <= 65535:
        return BootstrapRejected("unsupported_bind", "only the exact IPv4 loopback endpoint is supported")
    if resolve_auth_source() != "header":
        return BootstrapRejected("unsupported_auth", "this bootstrap supports the reviewed header authentication deployment only")
    implicit_local = not os.environ.get("OMNIGENT_AUTH_PROVIDER", "").strip() and "OMNIGENT_LOCAL_SINGLE_USER" not in os.environ
    if not local_single_user_enabled() and not implicit_local:
        return BootstrapRejected("unsupported_auth", "the guarded bootstrap requires the native callbacks' exact local single-user owner")
    provider = UnifiedAuthProvider(source="header", local_single_user=True)
    with httpx.Client(trust_env=False) as client:
        for with_binding in (False, True):
            headers = {"Authorization": "Bearer omo_native_bootstrap_probe"}
            if with_binding:
                headers["X-Omnigent-Runner-Tunnel-Token"] = "bootstrap-probe"
            request = client.build_request("POST", f"http://{options.host}:{options.port}/v1/sessions/bootstrap-probe/events", headers=headers, json={"type": "external_conversation_item", "data": {}})
            if provider.get_user_id(HTTPConnection({"type": "http", "headers": [(key.lower(), value) for key, value in request.headers.raw]})) != "local":
                return BootstrapRejected("unsupported_auth", "native callback headers must independently resolve to the exact local owner")
    cfg: dict[str, object] = {}
    if options.config_path is not None:
        try:
            loaded = yaml.safe_load(options.config_path.read_bytes()) or {}
        except (OSError, yaml.YAMLError):
            return BootstrapRejected("invalid_config", "cannot parse the requested server config")
        if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
            return BootstrapRejected("invalid_config", "server config must be a string-keyed mapping")
        cfg = loaded
    supported_keys = {
        "database_uri", "conversation_database_uri", "artifact_location", "execution_timeout",
        "admins", "allowed_domains", "policies", "policy_modules", "providers", "routing",
    }
    if set(cfg) - supported_keys:
        return BootstrapRejected("unsupported_config", "server config contains unreviewed settings")
    if cfg.get("routing") not in (None, {"provider": "none"}):
        return BootstrapRejected("unsupported_routing", "external or LLM routing is not supported by this bootstrap")
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        providers = load_provider_config().get("providers")
    if isinstance(providers, dict) and any(isinstance(value, dict) and value.get("kind") == "databricks" for value in providers.values()):
        return BootstrapRejected("unsupported_routing", "Databricks provider routing requires a separately reviewed bootstrap")
    conv_uri = options.conversation_database_uri or cfg.get("conversation_database_uri")
    if conv_uri is not None and not isinstance(conv_uri, str):
        return BootstrapRejected("invalid_storage", "conversation database URI must be a string")
    storage_paths = {options.fence_database.resolve()}
    for uri in [options.database_uri, conv_uri]:
        if uri is None:
            continue
        try:
            parsed = make_url(uri)
        except ArgumentError:
            return BootstrapRejected("invalid_storage", "cannot parse the configured database URI")
        if parsed.drivername != "sqlite" or not parsed.database or parsed.database == ":memory:" or not Path(parsed.database).is_absolute() or parsed.query:
            return BootstrapRejected("unsupported_storage", "explicit absolute SQLite database paths without URI query options are required")
        if Path(parsed.database).resolve() == options.fence_database.resolve():
            return BootstrapRejected("invalid_storage", "the fence database must be separate from the application databases")
        storage_paths.add(Path(parsed.database).resolve())
    if not Path(options.artifact_location).is_absolute() or not options.fence_database.is_absolute():
        return BootstrapRejected("unsupported_storage", "explicit absolute local artifact and fence paths are required")
    artifact_path = Path(options.artifact_location)
    if artifact_path.resolve() in storage_paths or artifact_path.exists() and not artifact_path.is_dir():
        return BootstrapRejected("invalid_storage", "artifact directory conflicts with a database or existing file")
    if options.replacement_control is not None:
        control = options.replacement_control
        if storage_paths.intersection(path.resolve() for path in (control.socket_path, control.preparation_path, control.packet_path, control.approval_path, control.backend_config_path)):
            return BootstrapRejected("invalid_control_path", "control artifacts must be separate from application and fence databases")
    timeout_s = options.execution_timeout_s if options.execution_timeout_s is not None else cfg.get("execution_timeout", 7200)
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s < 1:
        return BootstrapRejected("invalid_timeout", "execution timeout must be a positive number of seconds")
    if cfg.get("policy_modules") is not None and not isinstance(cfg["policy_modules"], list):
        return BootstrapRejected("invalid_config", "policy_modules must be a list")
    for key in ("admins", "allowed_domains", "policy_modules"):
        value = cfg.get(key)
        if value is not None and not (isinstance(value, str) or isinstance(value, list) and all(isinstance(item, str) for item in value)):
            return BootstrapRejected("invalid_config", "administrator, domain and policy module settings must contain strings")
    from omnigent.spec import parse_default_policies

    policies = cfg.get("policies")
    if policies is not None and not isinstance(policies, dict):
        return BootstrapRejected("invalid_policies", "default policies must be a mapping")
    try:
        default_policies = parse_default_policies(policies)
    except (TypeError, ValueError, OmnigentError):
        return BootstrapRejected("invalid_policies", "cannot parse the configured default policies")
    return ServerPlan(replace(options, conversation_database_uri=conv_uri), cfg, timeout_s, binding, default_policies)


def create_server(options: ServerOptions) -> ServerApplication | BootstrapRejected:
    """Construct the guarded app using public upstream components only."""
    plan = validate_options(options)
    if isinstance(plan, BootstrapRejected):
        return plan
    from sqlalchemy.engine import make_url
    from starlette.requests import HTTPConnection

    from omnigent.runtime import init as init_runtime
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider
    from omnigent.server.routing_backend import RoutingBackends
    from omnigent.server.server_config import config_str_list
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store import ConversationStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

    from omo_manager.omo_omnigent_fence import FenceGuard, FenceStore
    from omo_manager.omo_omnigent_ingress import NativeIngress

    cfg = plan.config
    resolved = plan.options
    if resolved.config_path:
        os.environ["OMNIGENT_CONFIG"] = str(resolved.config_path.resolve())
    if not os.environ.get("OMNIGENT_AUTH_PROVIDER", "").strip():
        os.environ.setdefault("OMNIGENT_LOCAL_SINGLE_USER", "1")
    os.environ["OMNIGENT_ACCOUNTS_AUTO_OPEN"] = "1"
    auth_provider = create_auth_provider()
    for uri in (resolved.database_uri, resolved.conversation_database_uri):
        if uri is not None:
            database = make_url(uri).database
            assert database is not None
            Path(database).parent.mkdir(parents=True, exist_ok=True)
    fence = FenceStore(resolved.fence_database)
    agent_store = SqlAlchemyAgentStore(resolved.database_uri, resolved.conversation_database_uri)
    file_store = SqlAlchemyFileStore(resolved.database_uri)
    conversation_store = GuardedConversationStore(SqlAlchemyConversationStore(resolved.database_uri, resolved.conversation_database_uri), fence)
    store_api = cast(ConversationStore, cast(object, conversation_store))
    comment_store = SqlAlchemyCommentStore(resolved.database_uri)
    policy_store = SqlAlchemyPolicyStore(resolved.database_uri)
    permission_store = SqlAlchemyPermissionStore(resolved.database_uri)
    scheduled_task_store = SqlAlchemyScheduledTaskStore(resolved.database_uri)
    project_store = SqlAlchemyProjectStore(resolved.database_uri)
    host_store = HostStore(resolved.database_uri)
    artifact_store = LocalArtifactStore(resolved.artifact_location)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=Path(resolved.artifact_location) / ".cache")
    caps = RuntimeCaps(
        execution_timeout=plan.execution_timeout_s,
        default_policies=plan.default_policies,
        routing_backends=RoutingBackends(),
    )
    init_runtime(
        conversation_store=store_api,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
        comment_store=comment_store,
        policy_store=policy_store,
        caps=caps,
    )
    tunnel_token = os.environ.get("OMNIGENT_RUNNER_TUNNEL_TOKEN")
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=store_api,
        comment_store=comment_store,
        policy_store=policy_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        runner_tunnel_tokens=frozenset({tunnel_token}) if tunnel_token else None,
        permission_store=permission_store,
        scheduled_task_store=scheduled_task_store,
        project_store=project_store,
        auth_provider=auth_provider,
        host_store=host_store,
        policy_modules=config_str_list(cfg.get("policy_modules")),
        admins=config_str_list(cfg.get("admins")),
        allowed_domains=config_str_list(cfg.get("allowed_domains")),
        server_config=cfg,
    )
    ingress = NativeIngress(
        fence,
        endpoint=(resolved.host, resolved.port),
        resolve_owner=store_api.get_session_owner,
        resolve_user=lambda scope: auth_provider.get_user_id(HTTPConnection(scope)),
    )
    return ServerApplication(FenceGuard(app, fence, ingress=ingress), app, conversation_store, fence, plan, ingress)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-uri", required=True)
    parser.add_argument("--artifact-location", required=True)
    parser.add_argument("--fence-database", required=True, type=Path)
    parser.add_argument("--conversation-database-uri")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--execution-timeout", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6767)
    parser.add_argument("--check", action="store_true", help="validate source and configuration without constructing stores or serving")
    parser.add_argument("--replacement-socket", type=Path)
    parser.add_argument("--replacement-preparation", type=Path)
    parser.add_argument("--replacement-packet", type=Path)
    parser.add_argument("--replacement-review", type=Path)
    parser.add_argument("--replacement-backend-config", type=Path)
    parser.add_argument("--replacement-preparation-sha256")
    args = parser.parse_args(argv)
    control_values = (args.replacement_socket, args.replacement_preparation, args.replacement_packet, args.replacement_review, args.replacement_backend_config, args.replacement_preparation_sha256)
    if any(value is not None for value in control_values) and any(value is None for value in control_values):
        parser.error("all six replacement control options are required together")
    control_options = ReplacementControlOptions(*control_values) if all(value is not None for value in control_values) else None
    options = ServerOptions(args.database_uri, args.artifact_location, args.fence_database, args.host, args.port, args.config, args.conversation_database_uri, args.execution_timeout, control_options)
    result = validate_options(options) if args.check else create_server(options)
    if isinstance(result, BootstrapRejected):
        print(f"{result.code}: {result.detail}", file=sys.stderr)
        return 2
    if isinstance(result, ServerPlan):
        print(f"validated OmniGent {OMNIGENT_VERSION} source {OMNIGENT_SOURCE_SHA256}")
        return 0
    serve(result)
    return 0


def serve(application: ServerApplication) -> None:
    """Serve in the main thread and signal stream shutdown before tunnel loss.

    Uvicorn installs its handlers before ASGI startup. Chaining them after
    successful startup preserves its exit handling while matching OmniGent's
    early shutdown marker without modifying the server implementation.
    """
    import uvicorn

    from omnigent.process_logging import configure_process_logging
    from omnigent.runner.transports.ws_tunnel.limits import (
        RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
    )
    from omnigent.runtime import session_stream, telemetry
    from omnigent.server import shutdown_state

    control: ReplacementControl | None = None
    shutdown_task: asyncio.Task[None] | None = None
    _ = configure_process_logging("server", logger_names=("omnigent", "uvicorn", "uvicorn.error", "uvicorn.access"))
    telemetry.init("omni-server")

    def begin_shutdown(signum: int, frame: FrameType | None) -> None:
        nonlocal shutdown_task
        if control is not None and shutdown_task is None:
            active_control = control
            active_control.stop_accepting()

            async def drained_shutdown() -> None:
                try:
                    await active_control.close()
                finally:
                    shutdown_state.mark_server_shutting_down()
                    session_stream.shutdown_all()
                    server.handle_exit(signum, frame)

            shutdown_task = asyncio.create_task(drained_shutdown())
            return
        shutdown_state.mark_server_shutting_down()
        session_stream.shutdown_all()
        server.handle_exit(signum, frame)

    async def shutdown_aware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await application.app(scope, receive, send)
            return

        async def lifespan_send(message: Message) -> None:
            nonlocal control
            if message["type"] == "lifespan.startup.complete":
                control_options = application.plan.options.replacement_control
                if control_options is not None:
                    from omo_manager.omo_omnigent_backend import BackendResources, create_backend
                    from omo_manager.omo_omnigent_fence import Rejected

                    def package_binding() -> str | Rejected:
                        result = validate_package()
                        return Rejected(result.code, result.detail) if isinstance(result, BootstrapRejected) else result

                    options = application.plan.options
                    resources = BackendResources(application.inner_app, application.fence_store, options.database_uri, options.conversation_database_uri, options.artifact_location, package_binding)
                    backend = create_backend(resources, asyncio.get_running_loop(), control_options.backend_config_path)
                    if isinstance(backend, Rejected):
                        raise RuntimeError(f"replacement backend startup rejected: {backend.code}")
                    if application.native_ingress is not None:
                        application.native_ingress.bind_native_resolver(backend.resolve_native_pin)
                    control = ReplacementControl(control_options, application, backend)
                    invalid = await control.start()
                    if invalid:
                        raise RuntimeError(f"replacement control startup rejected: {invalid.code}")
                for signum in (signal.SIGINT, signal.SIGTERM):
                    _ = signal.signal(signum, begin_shutdown)
            await send(message)

        async def lifespan_receive() -> Message:
            message = await receive()
            if message["type"] == "lifespan.shutdown":
                if shutdown_task is not None:
                    await asyncio.shield(shutdown_task)
                elif control is not None:
                    await control.close()
            return message

        await application.app(scope, lifespan_receive, lifespan_send)

    options = application.plan.options
    config = uvicorn.Config(
        shutdown_aware,
        host=options.host,
        port=options.port,
        log_config=None,
        ws_max_size=RUNNER_TUNNEL_MAX_MESSAGE_BYTES,
        ws_ping_interval=TUNNEL_KEEPALIVE_PING_INTERVAL_S,
        ws_ping_timeout=TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
        timeout_graceful_shutdown=int(os.environ.get("OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S", "5")),
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
