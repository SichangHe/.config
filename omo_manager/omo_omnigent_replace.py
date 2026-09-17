"""Exact Source1957 packets and fail-closed, crash-recoverable ownership transfer.

The production native-writer barrier is intentionally a required backend operation.
No HTTP stop acknowledgment is a quiescence proof. This module sends no mail.
"""

from __future__ import annotations

import base64
import argparse
import ast
import importlib
import json
import os
import re
import socket
import stat
import struct
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from functools import cache
from pathlib import Path
from typing import Literal, Protocol, cast

from omo_manager.omo_agent_status import DEFAULT_ROOT, LOCAL_ENV
from omo_manager.omo_manager_replace import ReplaceError, Snapshot, create_snapshot, file_identity, metadata, read_snapshot, replace_snapshot, replace_v1_fields
from omo_manager.omo_omnigent_fence import OMNIGENT_SOURCE_SHA256, ExpectedState, FenceStore, Operation, Rejected, ReplacementSpec, canonical, digest
from omo_manager.omo_task_audit import audit
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError

AUTHORITY_SHA256 = "840a51f8ffcc29761835f27bd88787475303f1e4df0ab260305c74bf06145a2a"
OLD_SESSION_ID = "a7e0eba316e044358c551762439af9d7"
OLD_THREAD_ID = "01a0ae03-b38e-7050-a988-b730f306b29b"
OLD_HOST_ID = "1b0c4985778d481d847e238605defa2f"
TASK_NAME = "webconf_autonomy2.md"
WORKSPACE_NAME = "DeGenTWeb_writeup"
REPORT_REQUIREMENT = 'Report directly to the Human when you assume responsibility and when you have a substantive result or blocker. Keep the existing email thread and exact subject "Re: Choose WebConf owner webconf_autonomy2.md".'
SOURCE_NAMES = frozenset(
    {
        "omo_omnigent_replace.py",
        "omo_omnigent_fence.py",
        "omo_omnigent_server.py",
        "omo_omnigent_backend.py",
        "omo_omnigent.py",
        "omo_task_lock.py",
        "omo_manager_replace.py",
        "omo_task_metadata.py",
        "omo_task_audit.py",
        "omo_agent_status.py",
    }
)
HASH = re.compile(r"[0-9a-f]{64}\Z")


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decoded(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


@cache
def implementation_sources() -> tuple[Path, ...]:
    """Bind a stable transitive set of local lifecycle implementation sources."""
    root = Path(__file__).resolve().parent
    paths: set[Path] = set()
    pending = [root / name for name in SOURCE_NAMES]
    while pending:
        path = pending.pop()
        if path in paths:
            continue
        paths.add(path)
        nodes = tuple(ast.walk(ast.parse(path.read_bytes(), filename=path.name)))
        imports = [node.module for node in nodes if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("omo_manager.")]
        imports.extend(alias.name for node in nodes if isinstance(node, ast.Import) for alias in node.names if alias.name.startswith("omo_manager."))
        imports.extend(f"omo_manager.{alias.name}" for node in nodes if isinstance(node, ast.ImportFrom) and node.module == "omo_manager" for alias in node.names)
        pending.extend(candidate for name in imports if (candidate := root.joinpath(*name.split(".")[1:]).with_suffix(".py")).is_file() and candidate not in paths)
    return tuple(sorted(paths))


@dataclass(frozen=True)
class FilePin:
    path: str
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int
    uid: int
    gid: int

    @classmethod
    def capture(cls, snapshot: Snapshot) -> FilePin:
        info = snapshot.state
        return cls(str(snapshot.path), digest(snapshot.data), *file_identity(info), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


@dataclass(frozen=True)
class FileImage:
    pin: FilePin
    data_b64: str

    @classmethod
    def capture(cls, path: Path) -> FileImage:
        snapshot = read_snapshot(path, path.name)
        return cls(FilePin.capture(snapshot), encoded(snapshot.data))


@dataclass(frozen=True)
class DirectoryPin:
    path: str
    device: int
    inode: int
    uid: int


@dataclass(frozen=True)
class WorkspacePin:
    path: str
    device: int
    inode: int
    uid: int
    ancestors: tuple[DirectoryPin, ...]

    @classmethod
    def capture(cls, path: Path) -> WorkspacePin:
        info = path.lstat()
        if not path.is_absolute() or path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("workspace must be an existing canonical directory owned by this user")
        ancestors: list[DirectoryPin] = []
        for parent in path.parents:
            state = parent.lstat()
            if not stat.S_ISDIR(state.st_mode):
                raise ValueError("workspace ancestors must be directories, never symlinks")
            ancestors.append(DirectoryPin(str(parent), state.st_dev, state.st_ino, state.st_uid))
        return cls(str(path), info.st_dev, info.st_ino, info.st_uid, tuple(ancestors))


def configured_workspace() -> Path:
    path = Path(LOCAL_ENV.get("OMO_SOURCE1957_WORKSPACE", ""))
    if not path.is_absolute() or path.resolve(strict=True) != path or path.name != WORKSPACE_NAME:
        raise ValueError("OMO_SOURCE1957_WORKSPACE must configure the exact canonical existing workspace")
    return path


def configuration_source() -> Path:
    """Use the same input selector as the existing manager environment loader."""
    path = Path(os.environ.get("OMO_MANAGER_LOCAL_ENV", Path.home() / ".config" / "omo_manager" / "local.env"))
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("the manager local configuration must be an existing canonical file")
    return path


@dataclass(frozen=True)
class ProcessPin:
    pid: int
    start_ticks: int
    boot_id: str
    argv_sha256: str
    writer_domain: str


@dataclass(frozen=True)
class RuntimeState:
    session_id: str
    runner_id: str
    host_id: str
    thread_id: str
    session_b64: str
    items_b64: str
    host_b64: str
    routing_b64: str
    processes: tuple[ProcessPin, ...]
    online_runner_ids: tuple[str, ...]
    pending_launch_ids: tuple[str, ...]
    routing_generation: int
    host_generation: int
    workspace: str


@dataclass(frozen=True)
class ManifestItem:
    queue_index: int
    queue_item_sha256: str
    disposition: Literal["open", "constraint"]
    text: str
    source_ref: str


@dataclass(frozen=True)
class HumanConstraint:
    source: FilePin
    quote: str


# 🧑 "Replace the current read-only owner atomically with one unrestricted OmniGent session in the existing DeGenTWeb_writeup directory, preserving the task and queue and delivering only open work."
@dataclass(frozen=True)
class ExactPacket:
    operation_id: str
    successor_id: str
    delivery_id: str
    authority: FileImage
    task: FileImage
    todo: FileImage
    workspace: WorkspacePin
    runtime: RuntimeState
    sources: tuple[FilePin, ...]
    package_sha256: str
    queue_b64: str
    queue: tuple[str, ...]
    manifest: tuple[ManifestItem, ...]
    constraints: tuple[HumanConstraint, ...]
    task_after_b64: str
    todo_after_b64: str
    prompt: str

    def serialize(self) -> bytes:
        return canonical(asdict(self)).encode()

    @property
    def sha256(self) -> str:
        return digest(self.serialize())

    @property
    def expected(self) -> ExpectedState:
        runtime = self.runtime
        return ExpectedState(
            session_id=runtime.session_id,
            runner_id=runtime.runner_id,
            host_id=runtime.host_id,
            thread_id=runtime.thread_id,
            task_sha256=self.task.pin.sha256,
            todo_sha256=self.todo.pin.sha256,
            queue_sha256=digest(decoded(self.queue_b64)),
            workspace=self.workspace.path,
            workspace_sha256=digest(canonical(asdict(self.workspace)).encode()),
            host_sha256=digest(decoded(runtime.host_b64)),
            process_sha256=digest(canonical([asdict(pin) for pin in runtime.processes]).encode()),
            routing_sha256=digest(decoded(runtime.routing_b64)),
            queue_count=len(self.queue),
            routing_generation=runtime.routing_generation,
            host_generation=runtime.host_generation,
        )

    @property
    def spec(self) -> ReplacementSpec:
        return ReplacementSpec(self.operation_id, self.successor_id, AUTHORITY_SHA256, self.sha256, self.expected)


@dataclass(frozen=True)
class ReviewApproval:
    packet_sha256: str
    evidence: FilePin


@dataclass(frozen=True)
class PreparationSnapshot:
    authority: FileImage
    task: FileImage
    todo: FileImage
    workspace: WorkspacePin
    sources: tuple[FilePin, ...]
    queue_b64: str
    authoring: tuple[FileImage, ...] = ()


@dataclass(frozen=True)
class Preparation:
    authority: Path
    task: Path
    todo: Path
    workspace: Path
    sources: tuple[Path, ...]
    package_sha256: str
    manifest: tuple[ManifestItem, ...]
    constraints: tuple[HumanConstraint, ...] = ()
    snapshot: PreparationSnapshot | None = None

    def serialize(self) -> bytes:
        return canonical(
            {**asdict(self), "authority": str(self.authority), "task": str(self.task), "todo": str(self.todo), "workspace": str(self.workspace), "sources": [str(path) for path in self.sources]}
        ).encode()


class ReplacementBackend(Protocol):
    """Trusted server integration, never client-supplied quiescence assertions.

    `ready` must reject until native writer-domain and pending-launch barriers,
    reserved-ID creation without launch, and inspectable single-use delivery are
    implemented and source-pinned. `observe` returns exact raw responses and a
    process census between two equal fence-generation reads. Lifecycle calls
    must CAS exact identities; their read-only `inspect_receipt` counterparts
    must independently prove results after a crash. Unknown delivery is terminal
    for automatic sending. No method may restart an entire host on census drift.
    """

    def ready(self, packet: ExactPacket) -> Rejected | None: ...
    def observe(self) -> RuntimeState | Rejected: ...
    def quiesce(self, packet: ExactPacket) -> dict[str, object] | Rejected: ...
    def prepare_successor(self, packet: ExactPacket) -> dict[str, object] | Rejected: ...
    def deliver(self, packet: ExactPacket) -> dict[str, object] | Rejected: ...
    def inspect_receipt(self, packet: ExactPacket, phase: str) -> dict[str, object] | Rejected: ...
    def reconcile_effect(self, packet: ExactPacket, phase: str) -> dict[str, object] | Rejected: ...


def raw_queue(data: bytes) -> bytes:
    lines = data.splitlines(keepends=True)
    end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == b"---")
    starts = [index for index, line in enumerate(lines[:end]) if line.startswith(b"pending_task_items:")]
    if len(starts) != 1:
        raise ValueError("one raw pending_task_items block is required")
    start = starts[0]
    stop = next((index for index in range(start + 1, end) if lines[index].strip() and not lines[index].startswith((b" ", b"\t", b"#"))), end)
    return b"".join(lines[start:stop])


def after_images(task: bytes, todo: bytes, root: Path, successor_id: str) -> tuple[bytes, bytes]:
    before = metadata(task, root, "old task")
    if before.version != "v1.0.0" or before.runat != f"omnigent://{OLD_SESSION_ID}" or before.is_manager or before.tool != "codex" or before.managerat != "dw:59":
        raise ValueError("Source1957 requires the exact v1 Codex worker")
    if before.status not in {"running", "blocked", "long_running"}:
        raise ValueError("Source1957 cannot replace a completed task")
    after = replace_v1_fields(task.decode(), status="running", runat=f"omnigent://{successor_id}", blocked_on="", remove_session=False).encode()
    if raw_queue(after) != raw_queue(task) or metadata(after, root, "successor task").pending_task_items != before.pending_task_items:
        raise ValueError("raw queue changed")
    lines = todo.splitlines(keepends=True)
    pattern = re.compile(rb"^(\s*" + re.escape(TASK_NAME.encode()) + rb"[ \t]+)(" + re.escape(before.runat.encode()) + rb")([ \t]*)(\r?\n)?$")
    matches = [(index, pattern.fullmatch(line)) for index, line in enumerate(lines) if TASK_NAME.encode() in line]
    if len(matches) != 1 or matches[0][1] is None:
        raise ValueError("TODO must have one exact old task route")
    index, match = matches[0]
    assert match is not None
    section = next((line.strip() for line in reversed(lines[:index]) if line.strip() in {b"current:", b"human pending:", b"low priority:", b"previous:"}), b"")
    if section not in {b"current:", b"human pending:"}:
        raise ValueError("old task must be in a live TODO section")
    lines[index] = match.group(1) + f"omnigent://{successor_id}".encode() + match.group(3) + (match.group(4) or b"")
    return after, b"".join(lines)


def build_prompt(manifest: tuple[ManifestItem, ...], constraints: tuple[HumanConstraint, ...]) -> str:
    open_work = "\n".join(f"- {item.text}" for item in manifest if item.disposition == "open")
    retained = "\n".join(f"- {item.text}" for item in manifest if item.disposition == "constraint")
    human = "\n".join(f"- {item.quote}" for item in constraints)
    return f"Open work reviewed for this replacement:\n{open_work}\n\nExisting constraints to preserve:\n{retained}\n{human}\n\n{REPORT_REQUIREMENT}\n"


def _read_pin(pin: FilePin) -> Snapshot:
    path = Path(pin.path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError(f"pinned file path or ancestor became noncanonical: {path.name}")
    snapshot = read_snapshot(path, path.name)
    if FilePin.capture(snapshot) != pin:
        raise ValueError(f"file identity or bytes changed: {Path(pin.path).name}")
    return snapshot


def validate(packet: ExactPacket, *, files: bool = True) -> Rejected | None:
    """Validate the exact scope and all derivations; never infer scientific completion."""
    try:
        for value in (packet.operation_id, packet.successor_id, packet.delivery_id):
            if uuid.UUID(hex=value).hex != value:
                raise ValueError("operation, successor and delivery IDs must be reserved UUID hex strings")
        if len({packet.operation_id, packet.successor_id, packet.delivery_id, OLD_SESSION_ID}) != 4:
            raise ValueError("reserved identities must be distinct")
        if digest(decoded(packet.authority.data_b64)) != AUTHORITY_SHA256 or packet.authority.pin.sha256 != AUTHORITY_SHA256:
            raise ValueError("authority must be exact Source1957")
        root = DEFAULT_ROOT.resolve(strict=True)
        if Path(packet.task.pin.path) != root / TASK_NAME or Path(packet.todo.pin.path) != root / "TODO.md":
            raise ValueError("task and TODO must be the exact configured Source1957 records")
        if (
            Path(packet.workspace.path) != configured_workspace()
            or packet.workspace.path != packet.runtime.workspace
            or packet.runtime.session_id != OLD_SESSION_ID
            or packet.runtime.thread_id != OLD_THREAD_ID
            or packet.runtime.host_id != OLD_HOST_ID
        ):
            raise ValueError("authority does not cover this owner, host, thread or workspace")
        if packet.runtime.online_runner_ids != (packet.runtime.runner_id,) or packet.runtime.pending_launch_ids or not packet.runtime.processes:
            raise ValueError("old host census must have exactly the old runner, known native writers and no pending launches")
        if any(pin.pid <= 0 or pin.start_ticks <= 0 or not pin.boot_id or not pin.writer_domain or not HASH.fullmatch(pin.argv_sha256) for pin in packet.runtime.processes):
            raise ValueError("native process pins are incomplete")
        if not {*implementation_sources(), configuration_source()}.issubset({Path(pin.path) for pin in packet.sources}) or packet.package_sha256 != OMNIGENT_SOURCE_SHA256:
            raise ValueError("coordinator, publication, parser, fence, server and installed package sources must be pinned")
        task, todo = decoded(packet.task.data_b64), decoded(packet.todo.data_b64)
        if digest(task) != packet.task.pin.sha256 or digest(todo) != packet.todo.pin.sha256:
            raise ValueError("file bytes do not match their pins")
        if b"(pending)" in {line.strip() for line in task.splitlines()}:
            raise ValueError("unrecorded dispatch markers would replay old input after transfer")
        queue = metadata(task, Path(packet.task.pin.path).parent, "task").pending_task_items
        if packet.queue != queue or len(queue) != 9 or decoded(packet.queue_b64) != raw_queue(task):
            raise ValueError("all nine original queue items and raw bytes must be retained")
        if tuple(item.queue_index for item in packet.manifest) != tuple(range(9)) or not any(item.disposition == "open" for item in packet.manifest):
            raise ValueError("reviewed manifest must account for each queue item in order and name open work")
        for item in packet.manifest:
            if item.disposition not in {"open", "constraint"} or digest(queue[item.queue_index].encode()) != item.queue_item_sha256 or not item.text.strip() or not item.source_ref.strip():
                raise ValueError("manifest entry lacks its exact queue binding, reviewed work or source")
        after = after_images(task, todo, Path(packet.task.pin.path).parent, packet.successor_id)
        if after != (decoded(packet.task_after_b64), decoded(packet.todo_after_b64)) or packet.prompt != build_prompt(packet.manifest, packet.constraints):
            raise ValueError("ownership after-images or reviewed prompt changed")
        for value in (packet.runtime.session_b64, packet.runtime.items_b64, packet.runtime.host_b64, packet.runtime.routing_b64):
            if not decoded(value):
                raise ValueError("runtime raw evidence is empty")
        if files:
            for pin in (packet.authority.pin, *packet.sources):
                _ = _read_pin(pin)
            if WorkspacePin.capture(Path(packet.workspace.path)) != packet.workspace:
                raise ValueError("workspace directory identity changed")
            for constraint in packet.constraints:
                if not constraint.quote or constraint.quote.encode() not in _read_pin(constraint.source).data:
                    raise ValueError("Human constraint must quote its exact pinned source")
    except (OSError, ValueError, ReplaceError, TaskFrontmatterError, StopIteration) as exc:
        return Rejected("packet_changed_or_invalid", str(exc))
    return None


@contextmanager
def ownership_locks(task: Path, todo: Path, successor_id: str) -> Iterator[None]:
    with ExitStack() as stack:
        for target in sorted((f"omnigent://{OLD_SESSION_ID}", f"omnigent://{successor_id}")):
            stack.enter_context(task_target_lock(task.parent, target, timeout_s=5))
        for path in sorted((task, todo)):
            stack.enter_context(task_file_lock(path, timeout_s=5))
        yield


def _owner_audit(packet: ExactPacket) -> Rejected | None:
    """Use the supported read-only audit for exact owner/index conflicts."""
    targets = {f"omnigent://{OLD_SESSION_ID}", f"omnigent://{packet.successor_id}"}
    try:
        findings = tuple(finding for finding in audit(Path(packet.task.pin.path).parent) if TASK_NAME in finding.tasks or finding.key in targets)
        if findings:
            return Rejected("task_ownership_conflict", canonical([asdict(finding) for finding in findings]))
    except (OSError, ValueError, TaskFrontmatterError) as exc:
        return Rejected("task_audit_failed", str(exc))
    return None


def prepare(inputs: Preparation, store: FenceStore, backend: ReplacementBackend) -> ExactPacket | Rejected:
    """Reserve identities and persist a complete review packet without lifecycle work."""
    successor_id = uuid.uuid4().hex
    try:
        with ownership_locks(inputs.task, inputs.todo, successor_id):
            invalid = validate_preparation(inputs)
            if invalid:
                return invalid
            snapshot = inputs.snapshot
            assert snapshot is not None
            generation = store.generation(OLD_SESSION_ID)
            runtime = backend.observe()
            if isinstance(runtime, Rejected):
                return runtime
            host_generation = store.host_generation(runtime.host_id)
            task, todo = snapshot.task, snapshot.todo
            task_after, todo_after = after_images(decoded(task.data_b64), decoded(todo.data_b64), inputs.task.parent, successor_id)
            packet = ExactPacket(
                uuid.uuid4().hex,
                successor_id,
                uuid.uuid4().hex,
                snapshot.authority,
                task,
                todo,
                snapshot.workspace,
                runtime,
                snapshot.sources,
                inputs.package_sha256,
                encoded(raw_queue(decoded(task.data_b64))),
                metadata(decoded(task.data_b64), inputs.task.parent, "task").pending_task_items,
                inputs.manifest,
                inputs.constraints,
                encoded(task_after),
                encoded(todo_after),
                build_prompt(inputs.manifest, inputs.constraints),
            )
            invalid = validate(packet) or _owner_audit(packet)
            if invalid:
                return invalid
            if (
                runtime.routing_generation != generation
                or store.generation(OLD_SESSION_ID) != generation
                or runtime.host_generation != host_generation
                or store.host_generation(runtime.host_id) != host_generation
            ):
                return Rejected("routing_changed", "admission generation changed during preparation")
            invalid = validate_preparation(inputs)
            if invalid:
                return invalid
            result = store.prepare(packet.spec, packet.expected, decoded(packet.authority.data_b64), packet.serialize())
            return result if isinstance(result, Rejected) else packet
    except (OSError, ValueError, ReplaceError, TaskFrontmatterError) as exc:
        return Rejected("preparation_failed", str(exc))


def inspect(packet: ExactPacket, store: FenceStore) -> Operation | Rejected:
    operation = store.get(packet.operation_id)
    if operation is None or operation.spec != packet.spec:
        return Rejected("operation_packet_mismatch", "the durable reservation differs from this exact packet")
    return operation


def _review(packet: ExactPacket, approval: ReviewApproval) -> Rejected | None:
    try:
        if approval.packet_sha256 != packet.sha256 or approval.evidence.path in {packet.authority.pin.path, packet.task.pin.path, packet.todo.pin.path}:
            raise ValueError("a separate exact-packet review is required")
        evidence = _read_pin(approval.evidence).data
        if f"approve Source1957 replacement {packet.sha256}".encode() not in evidence.splitlines():
            raise ValueError("review evidence must explicitly approve this exact packet digest")
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("review_required", str(exc))
    return None


def _receipt(packet: ExactPacket, phase: str, value: dict[str, object]) -> Rejected | None:
    if value.get("operation_id") != packet.operation_id or not HASH.fullmatch(str(value.get("evidence_sha256", ""))):
        return Rejected("unbound_receipt", phase)
    if phase == "old_quiesced":
        expected = packet.expected
        bindings = {"session_id": expected.session_id, "runner_id": expected.runner_id, "thread_id": expected.thread_id, "process_sha256": expected.process_sha256, "host_sha256": expected.host_sha256}
        if any(value.get(key) != expected for key, expected in bindings.items()) or any(value.get(key) is not True for key in ("native_writers_gone", "pending_launches_absent", "transport_drained")):
            return Rejected("invalid_quiescence", "native writers, launch census and transport need bound proof")
    elif value.get("new_session_id") != packet.successor_id:
        return Rejected("wrong_successor", phase)
    if phase == "new_prepared" and (
        value.get("workspace") != packet.workspace.path or value.get("unrestricted") is not True or value.get("message_count") != 0 or value.get("runner_absent") is not True
    ):
        return Rejected("unsafe_successor", "reserved successor must be unrestricted and empty with no runner")
    if phase == "delivery_confirmed" and (value.get("delivery_id") != packet.delivery_id or value.get("prompt_sha256") != digest(packet.prompt.encode())):
        return Rejected("wrong_delivery", "confirmation must bind the only outbox ID and exact reviewed prompt")
    return None


def _evidence(packet: ExactPacket, **values: object) -> dict[str, object]:
    receipt = {"operation_id": packet.operation_id, **values}
    return receipt | {"evidence_sha256": digest(canonical(receipt).encode())}


def _rollback_files(packet: ExactPacket, store: FenceStore, completed: list[tuple[str, Snapshot, bytes]]) -> None:
    """Restore each independently owned file even if another path was rebound."""
    for name, published, before in reversed(completed):
        try:
            restored = replace_snapshot(published, before, f"rollback {name}")
            _ = store.record_evidence(packet.operation_id, f"file_{name}_rollback", _evidence(packet, pin=asdict(FilePin.capture(restored))))
        except (OSError, ValueError, ReplaceError):
            continue


def _publish(packet: ExactPacket, store: FenceStore) -> Operation | Rejected:
    """Publish under both file locks; incomplete exchange receipts stay uncertain."""
    completed: list[tuple[str, Snapshot, bytes]] = []
    for name, image, after_b64 in (("task", packet.task, packet.task_after_b64), ("todo", packet.todo, packet.todo_after_b64)):
        operation = inspect(packet, store)
        if isinstance(operation, Rejected):
            return operation
        done = operation.receipts.get(f"file_{name}_done")
        try:
            for _prior_name, published, _before in completed:
                _ = _read_pin(FilePin.capture(published))
            current = read_snapshot(Path(image.pin.path), name)
            if done:
                if done.get("pin") != asdict(FilePin.capture(current)) or current.data != decoded(after_b64):
                    raise ValueError(f"published {name} changed")
                completed.append((name, current, decoded(image.data_b64)))
                continue
            if f"file_{name}_intent" in operation.receipts:
                raise ValueError(f"{name} exchange lacks its durable inode receipt; independent reconciliation required")
            if FilePin.capture(current) != image.pin:
                raise ValueError(f"{name} identity or bytes changed before publication")
            intent = store.record_evidence(packet.operation_id, f"file_{name}_intent", _evidence(packet, before=asdict(image.pin), after_sha256=digest(decoded(after_b64))))
            if isinstance(intent, Rejected):
                return intent
            published = replace_snapshot(current, decoded(after_b64), name)
            completed.append((name, published, decoded(image.data_b64)))
            result = store.record_evidence(packet.operation_id, f"file_{name}_done", _evidence(packet, pin=asdict(FilePin.capture(published))))
            if isinstance(result, Rejected):
                raise ValueError(result.detail)
            for _prior_name, prior, _before in completed:
                _ = _read_pin(FilePin.capture(prior))
        except (OSError, ValueError, ReplaceError) as exc:
            _ = store.mark_uncertain(packet.operation_id, str(exc))
            _rollback_files(packet, store, completed)
            return Rejected("publication_uncertain", str(exc))
    invalid = _confirmed_files(packet, store) or _owner_audit(packet)
    if invalid:
        _ = store.mark_uncertain(packet.operation_id, invalid.detail)
        _rollback_files(packet, store, completed)
        return invalid
    return store.advance(
        packet.operation_id, "committed", _evidence(packet, new_session_id=packet.successor_id, task_sha256=digest(decoded(packet.task_after_b64)), todo_sha256=digest(decoded(packet.todo_after_b64)))
    )


def _confirmed_files(packet: ExactPacket, store: FenceStore) -> Rejected | None:
    operation = inspect(packet, store)
    if isinstance(operation, Rejected):
        return operation
    try:
        for name, image, after_b64 in (("task", packet.task, packet.task_after_b64), ("todo", packet.todo, packet.todo_after_b64)):
            current = read_snapshot(Path(image.pin.path), name)
            if operation.receipts.get(f"file_{name}_done", {}).get("pin") != asdict(FilePin.capture(current)) or current.data != decoded(after_b64):
                raise ValueError(f"committed {name} ownership changed")
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("committed_files_changed", str(exc))
    return None


def execute(packet: ExactPacket, approval: ReviewApproval, store: FenceStore, backend: ReplacementBackend) -> Operation | Rejected:
    """Run an approved packet once; missing server barriers reject before fencing."""
    invalid = validate(packet) or _review(packet, approval)
    if invalid:
        return invalid
    with ownership_locks(Path(packet.task.pin.path), Path(packet.todo.pin.path), packet.successor_id):
        invalid = validate(packet) or _review(packet, approval)
        if invalid:
            return invalid
        operation = inspect(packet, store)
        if isinstance(operation, Rejected):
            return operation
        if operation.phase == "delivery_confirmed":
            return operation
        if operation.status != "active":
            return Rejected("reconciliation_required", operation.status)
        reviewed = store.record_evidence(packet.operation_id, "exact_review", _evidence(packet, packet_sha256=packet.sha256, approval=asdict(approval.evidence)))
        if isinstance(reviewed, Rejected):
            return reviewed
        ready = backend.ready(packet)
        if ready:
            return ready
        if operation.phase == "prepared":
            try:
                _ = _read_pin(packet.task.pin)
                _ = _read_pin(packet.todo.pin)
            except (OSError, ValueError, ReplaceError) as exc:
                return Rejected("ownership_changed", str(exc))
            observed = backend.observe()
            if isinstance(observed, Rejected):
                return observed
            if observed != packet.runtime:
                return Rejected("runtime_changed", "session, items, host, process or routing evidence drifted")
            invalid = _owner_audit(packet)
            if invalid:
                return invalid
            operation = store.fence(packet.operation_id, packet.expected)
            if isinstance(operation, Rejected):
                return operation
        phases = {"fenced": ("old_quiesced", backend.quiesce), "old_quiesced": ("new_prepared", backend.prepare_successor), "committed": ("delivery_confirmed", backend.deliver)}
        while operation.phase != "delivery_confirmed":
            if operation.phase == "new_prepared":
                operation = _publish(packet, store)
                if isinstance(operation, Rejected):
                    return operation
                continue
            phase, action = phases[operation.phase]
            saved = operation.receipts.get(f"result_{phase}")
            if saved:
                invalid = _receipt(packet, phase, saved)
                if invalid:
                    return invalid
                if phase == "delivery_confirmed" and (invalid := _confirmed_files(packet, store) or _owner_audit(packet)):
                    _ = store.mark_uncertain(packet.operation_id, invalid.detail)
                    return invalid
                if phase == "old_quiesced":
                    drained = store.reconcile_remote_admissions(packet.operation_id, saved)
                    if isinstance(drained, Rejected):
                        return drained
                operation = store.advance(packet.operation_id, phase, saved)
                if isinstance(operation, Rejected):
                    return operation
                continue
            if phase == "old_quiesced" and any(not admission.kind.startswith("remote:") for admission in store.active_admissions(OLD_SESSION_ID)):
                return Rejected("admissions_outstanding", "old HTTP/store writers have not drained")
            if phase == "delivery_confirmed":
                invalid = _confirmed_files(packet, store) or _owner_audit(packet)
                if invalid:
                    _ = store.mark_uncertain(packet.operation_id, invalid.detail)
                    return invalid
                claimed = store.claim_delivery(packet.operation_id, packet.delivery_id, digest(packet.prompt.encode()))
                if isinstance(claimed, Rejected):
                    if claimed.code == "delivery_already_claimed":
                        _ = store.mark_uncertain(packet.operation_id, "delivery intent already exists; inspect the only outbox ID")
                    return claimed
            uncertain = store.mark_uncertain(packet.operation_id, f"{phase} started; crash requires independent inspection")
            if isinstance(uncertain, Rejected):
                return uncertain
            receipt = action(packet)
            if isinstance(receipt, Rejected):
                return receipt
            invalid = _receipt(packet, phase, receipt)
            if invalid:
                return invalid
            if phase == "delivery_confirmed" and (invalid := _confirmed_files(packet, store) or _owner_audit(packet)):
                return invalid
            recorded = store.record_evidence(packet.operation_id, f"result_{phase}", receipt)
            if isinstance(recorded, Rejected):
                return recorded
            resolved = store.reconcile(packet.operation_id, _evidence(packet, resolved=True, phase=phase))
            if isinstance(resolved, Rejected):
                return resolved
            if phase == "old_quiesced":
                drained = store.reconcile_remote_admissions(packet.operation_id, receipt)
                if isinstance(drained, Rejected):
                    return drained
            operation = store.advance(packet.operation_id, phase, receipt)
            if isinstance(operation, Rejected):
                return operation
        return operation


def reconcile(packet: ExactPacket, approval: ReviewApproval, store: FenceStore, backend: ReplacementBackend) -> Operation | Rejected:
    """Inspect uncertainty or finish a proven owned successor's initialization.

    Owned successor initialization and proven pre-dispatch staging may resume.
    Once `manifest_dispatch_intent` exists, sending is never attempted again.
    File publication and uncertain native stops are never retried.
    """
    invalid = validate(packet) or _review(packet, approval)
    if invalid:
        return invalid
    with ownership_locks(Path(packet.task.pin.path), Path(packet.todo.pin.path), packet.successor_id):
        invalid = validate(packet) or _review(packet, approval)
        if invalid:
            return invalid
        operation = inspect(packet, store)
        if isinstance(operation, Rejected):
            return operation
        if operation.status == "active" and operation.phase == "committed" and "delivery_intent" in operation.receipts:
            operation = store.mark_uncertain(packet.operation_id, "inspect a previously claimed outbox ID")
            if isinstance(operation, Rejected):
                return operation
        if operation.status == "active":
            return operation
        phases = {"fenced": "old_quiesced", "old_quiesced": "new_prepared", "committed": "delivery_confirmed"}
        phase = phases.get(operation.phase)
        if phase is None:
            return Rejected("file_reconciliation_required", "publication lacks an exact receipt; no automatic ownership guess")
        receipt = backend.inspect_receipt(packet, phase)
        if isinstance(receipt, Rejected) and phase == "new_prepared" and receipt.code == "owned_successor_incomplete":
            receipt = backend.reconcile_effect(packet, phase)
        elif isinstance(receipt, Rejected) and phase == "delivery_confirmed" and receipt.code == "owned_delivery_pre_dispatch":
            latest = inspect(packet, store)
            if isinstance(latest, Rejected):
                return latest
            if "manifest_dispatch_intent" in latest.receipts:
                return Rejected("delivery_dispatch_already_intended", "inspect the existing manifest; never resend")
            invalid = _confirmed_files(packet, store) or _owner_audit(packet)
            if invalid:
                return invalid
            receipt = backend.reconcile_effect(packet, phase)
        if isinstance(receipt, Rejected):
            return receipt
        invalid = _receipt(packet, phase, receipt)
        if invalid:
            return invalid
        if phase == "delivery_confirmed" and (invalid := _confirmed_files(packet, store) or _owner_audit(packet)):
            return invalid
        resolved = store.reconcile(packet.operation_id, _evidence(packet, resolved=True, phase=phase, verified_receipt=receipt))
        if isinstance(resolved, Rejected):
            return resolved
        if phase == "old_quiesced":
            drained = store.reconcile_remote_admissions(packet.operation_id, receipt)
            if isinstance(drained, Rejected):
                return drained
        return store.advance(packet.operation_id, phase, receipt)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    result = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in result):
        raise ValueError("object keys must be strings")
    return cast(dict[str, object], result)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected an array")
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise ValueError("expected an integer")
    return value


def _pin(value: object) -> FilePin:
    item = _object(value)
    return FilePin(
        _text(item["path"]),
        _text(item["sha256"]),
        _integer(item["device"]),
        _integer(item["inode"]),
        _integer(item["size"]),
        _integer(item["mtime_ns"]),
        _integer(item["ctime_ns"]),
        _integer(item["mode"]),
        _integer(item["uid"]),
        _integer(item["gid"]),
    )


def _image(value: object) -> FileImage:
    item = _object(value)
    return FileImage(_pin(item["pin"]), _text(item["data_b64"]))


def _workspace(value: object) -> WorkspacePin:
    item = _object(value)
    parents = tuple(_object(parent) for parent in _array(item["ancestors"]))
    return WorkspacePin(
        _text(item["path"]),
        _integer(item["device"]),
        _integer(item["inode"]),
        _integer(item["uid"]),
        tuple(DirectoryPin(_text(parent["path"]), _integer(parent["device"]), _integer(parent["inode"]), _integer(parent["uid"])) for parent in parents),
    )


def load_packet(data: bytes) -> ExactPacket | Rejected:
    """Read typed canonical bytes; round-trip equality also rejects unknown keys."""
    try:
        value = _object(cast(object, json.loads(data)))
        runtime = _object(value["runtime"])
        process_rows = tuple(_object(item) for item in _array(runtime["processes"]))
        processes = tuple(ProcessPin(_integer(item["pid"]), _integer(item["start_ticks"]), _text(item["boot_id"]), _text(item["argv_sha256"]), _text(item["writer_domain"])) for item in process_rows)
        manifest: list[ManifestItem] = []
        for row in _array(value["manifest"]):
            item = _object(row)
            disposition = _text(item["disposition"])
            if disposition not in {"open", "constraint"}:
                raise ValueError("manifest disposition must be open or constraint")
            manifest.append(
                ManifestItem(_integer(item["queue_index"]), _text(item["queue_item_sha256"]), cast(Literal["open", "constraint"], disposition), _text(item["text"]), _text(item["source_ref"]))
            )
        constraint_rows = tuple(_object(item) for item in _array(value["constraints"]))
        packet = ExactPacket(
            _text(value["operation_id"]),
            _text(value["successor_id"]),
            _text(value["delivery_id"]),
            _image(value["authority"]),
            _image(value["task"]),
            _image(value["todo"]),
            _workspace(value["workspace"]),
            RuntimeState(
                _text(runtime["session_id"]),
                _text(runtime["runner_id"]),
                _text(runtime["host_id"]),
                _text(runtime["thread_id"]),
                _text(runtime["session_b64"]),
                _text(runtime["items_b64"]),
                _text(runtime["host_b64"]),
                _text(runtime["routing_b64"]),
                processes,
                tuple(_text(item) for item in _array(runtime["online_runner_ids"])),
                tuple(_text(item) for item in _array(runtime["pending_launch_ids"])),
                _integer(runtime["routing_generation"]),
                _integer(runtime["host_generation"]),
                _text(runtime["workspace"]),
            ),
            tuple(_pin(item) for item in _array(value["sources"])),
            _text(value["package_sha256"]),
            _text(value["queue_b64"]),
            tuple(_text(item) for item in _array(value["queue"])),
            tuple(manifest),
            tuple(HumanConstraint(_pin(item["source"]), _text(item["quote"])) for item in constraint_rows),
            _text(value["task_after_b64"]),
            _text(value["todo_after_b64"]),
            _text(value["prompt"]),
        )
        if packet.serialize() != data:
            raise ValueError("packet must use exact canonical bytes")
        return validate(packet, files=False) or packet
    except (ValueError, TypeError, KeyError) as exc:
        return Rejected("invalid_packet_encoding", str(exc))


def installed_package_digest() -> str | Rejected:
    verifier = cast(Callable[[], object], getattr(importlib.import_module("omo_manager.omo_omnigent_server"), "validate_package"))
    result = verifier()
    return result if isinstance(result, str) else Rejected("installed_package_unverified", str(result))


def validate_preparation(inputs: Preparation) -> Rejected | None:
    """Reject any captured identity drift before reserving a replacement."""
    try:
        snapshot = inputs.snapshot
        if snapshot is None:
            raise ValueError("preparation requires its serialized immutable snapshot")
        if inputs.task != DEFAULT_ROOT.resolve(strict=True) / TASK_NAME or inputs.todo != inputs.task.parent / "TODO.md" or inputs.workspace != configured_workspace():
            raise ValueError("preparation must bind the configured task, TODO and exact workspace")
        if tuple(image.pin.path for image in (snapshot.authority, snapshot.task, snapshot.todo)) != tuple(str(path) for path in (inputs.authority, inputs.task, inputs.todo)):
            raise ValueError("serialized file images differ from preparation paths")
        if tuple(pin.path for pin in snapshot.sources) != tuple(str(path) for path in inputs.sources) or len(set(inputs.sources)) != len(inputs.sources):
            raise ValueError("every source path needs one exact serialized pin")
        if not {*implementation_sources(), configuration_source()}.issubset(inputs.sources):
            raise ValueError("preparation must pin all implementation sources and manager local configuration")
        for image in (snapshot.authority, snapshot.task, snapshot.todo, *snapshot.authoring):
            if digest(decoded(image.data_b64)) != image.pin.sha256 or _read_pin(image.pin).data != decoded(image.data_b64):
                raise ValueError("serialized file image or current bytes changed")
        if snapshot.authority.pin.sha256 != AUTHORITY_SHA256:
            raise ValueError("authority must be exact Source1957")
        if snapshot.workspace.path != str(inputs.workspace) or WorkspacePin.capture(inputs.workspace) != snapshot.workspace:
            raise ValueError("workspace, owner or ancestor identities changed")
        task_bytes = decoded(snapshot.task.data_b64)
        if decoded(snapshot.queue_b64) != raw_queue(task_bytes):
            raise ValueError("serialized raw queue differs from the task image")
        _ = after_images(task_bytes, decoded(snapshot.todo.data_b64), inputs.task.parent, "0" * 32)
        queue = metadata(task_bytes, inputs.task.parent, "prepared task").pending_task_items
        if len(queue) != 9 or tuple(item.queue_index for item in inputs.manifest) != tuple(range(9)):
            raise ValueError("preparation must retain the complete ordered nine-item queue")
        if b"(pending)" in {line.strip() for line in task_bytes.splitlines()} or not any(item.disposition == "open" for item in inputs.manifest):
            raise ValueError("preparation requires recorded open work without undispatched history")
        for item in inputs.manifest:
            if item.queue_item_sha256 != digest(queue[item.queue_index].encode()) or item.disposition not in {"open", "constraint"} or not item.text.strip() or not item.source_ref.strip():
                raise ValueError("reviewed manifest no longer matches the captured queue")
        for pin in snapshot.sources:
            _ = _read_pin(pin)
        for constraint in inputs.constraints:
            if not constraint.quote or constraint.quote.encode() not in _read_pin(constraint.source).data:
                raise ValueError("constraint source or exact quote changed")
        package = installed_package_digest()
        if isinstance(package, Rejected):
            return package
        if package != inputs.package_sha256 or package != OMNIGENT_SOURCE_SHA256:
            raise ValueError("installed package digest changed")
    except (OSError, ValueError, ReplaceError, TaskFrontmatterError, StopIteration) as exc:
        return Rejected("preparation_snapshot_changed", str(exc))
    return None


def bind_preparation(inputs: Preparation, *, authoring: tuple[FileImage, ...] = ()) -> Preparation | Rejected:
    """Explicitly capture inputs once; readers never call this authoring operation."""
    try:
        sources = tuple(sorted({*inputs.sources, configuration_source()}))
        task = FileImage.capture(inputs.task)
        snapshot = PreparationSnapshot(
            FileImage.capture(inputs.authority),
            task,
            FileImage.capture(inputs.todo),
            WorkspacePin.capture(inputs.workspace),
            tuple(FileImage.capture(path).pin for path in sources),
            encoded(raw_queue(decoded(task.data_b64))),
            authoring,
        )
        bound = replace(inputs, sources=sources, snapshot=snapshot)
        return validate_preparation(bound) or bound
    except (OSError, ValueError, ReplaceError, TaskFrontmatterError, StopIteration) as exc:
        return Rejected("preparation_binding_failed", str(exc))


def load_preparation(data: bytes) -> Preparation | Rejected:
    """Load and check the exact serialized snapshot without adopting current bytes."""
    try:
        value = _object(cast(object, json.loads(data)))
        allowed = {"authority", "task", "todo", "workspace", "sources", "package_sha256", "manifest", "constraints", "snapshot"}
        if set(value) != allowed:
            raise ValueError("preparation fields must match the exact Source1957 schema")
        manifest: list[ManifestItem] = []
        for row in _array(value["manifest"]):
            item = _object(row)
            disposition = _text(item["disposition"])
            if disposition not in {"open", "constraint"} or set(item) != {"queue_index", "queue_item_sha256", "disposition", "text", "source_ref"}:
                raise ValueError("invalid reviewed manifest entry")
            manifest.append(
                ManifestItem(_integer(item["queue_index"]), _text(item["queue_item_sha256"]), cast(Literal["open", "constraint"], disposition), _text(item["text"]), _text(item["source_ref"]))
            )
        constraints: list[HumanConstraint] = []
        for row in _array(value["constraints"]):
            item = _object(row)
            if set(item) != {"source", "quote"}:
                raise ValueError("constraint must name its source file and exact quote")
            constraints.append(HumanConstraint(_pin(item["source"]), _text(item["quote"])))
        snapshot = _object(value["snapshot"])
        inputs = Preparation(
            Path(_text(value["authority"])),
            Path(_text(value["task"])),
            Path(_text(value["todo"])),
            Path(_text(value["workspace"])),
            tuple(Path(_text(path)) for path in _array(value["sources"])),
            _text(value["package_sha256"]),
            tuple(manifest),
            tuple(constraints),
            PreparationSnapshot(
                _image(snapshot["authority"]),
                _image(snapshot["task"]),
                _image(snapshot["todo"]),
                _workspace(snapshot["workspace"]),
                tuple(_pin(pin) for pin in _array(snapshot["sources"])),
                _text(snapshot["queue_b64"]),
                tuple(_image(image) for image in _array(snapshot["authoring"])),
            ),
        )
        if inputs.serialize() != data:
            raise ValueError("preparation must contain exact canonical snapshot bytes")
        return validate_preparation(inputs) or inputs
    except (OSError, ValueError, KeyError, ReplaceError) as exc:
        return Rejected("invalid_preparation", str(exc))


def build_preparation(
    *,
    authority: Path,
    task: Path,
    todo: Path,
    workspace: Path,
    manifest: Path,
    output: Path,
    constraints: Path | None = None,
    bound_files: tuple[Path, ...] = (),
) -> dict[str, object] | Rejected:
    """Author one private preparation artifact without reserving or changing owners.

    The manifest is an array of `queue_index`, `disposition`, `text`, `source_ref`
    objects. Optional constraints are an array of exact `source`, `quote` objects.
    Every captured image and identity is serialized and checked before reservation.
    """
    try:
        parent = output.parent
        parent_info = parent.stat()
        root = DEFAULT_ROOT.resolve(strict=True)
        if not output.is_absolute() or parent.resolve(strict=True) != parent or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700 or root in output.parents:
            return Rejected("unsafe_preparation_output", "output requires an existing canonical owned 0700 directory outside the work logs")
        paths = (authority, task, todo, workspace, manifest, *bound_files, *((constraints,) if constraints is not None else ()))
        if any(not path.is_absolute() or path.resolve(strict=True) != path for path in paths):
            return Rejected("noncanonical_preparation_input", "input and bound-file paths must be canonical absolute paths")
        if task != root / TASK_NAME or todo != root / "TODO.md":
            return Rejected("wrong_preparation_owner", "task and TODO must be the exact configured Source1957 records")
        workspace_pin = WorkspacePin.capture(workspace)
        if Path(workspace_pin.path) != configured_workspace():
            return Rejected("wrong_preparation_workspace", "Source1957 requires the existing DeGenTWeb_writeup directory")
        snapshots = [FileImage.capture(path) for path in (authority, task, todo, manifest)]
        authority_image, task_image, todo_image, manifest_image = snapshots
        if authority_image.pin.sha256 != AUTHORITY_SHA256:
            return Rejected("wrong_preparation_authority", "authority bytes must match exact Source1957")
        task_bytes = decoded(task_image.data_b64)
        _ = after_images(task_bytes, decoded(todo_image.data_b64), root, uuid.uuid4().hex)
        if b"(pending)" in {line.strip() for line in task_bytes.splitlines()}:
            return Rejected("unrecorded_dispatch", "record pending work before building replacement inputs")
        queue = metadata(task_bytes, root, "Source1957 task").pending_task_items
        if len(queue) != 9:
            return Rejected("queue_changed", "Source1957 requires all nine original queue items")
        rows = tuple(_object(row) for row in _array(cast(object, json.loads(decoded(manifest_image.data_b64)))))
        if tuple(_integer(row["queue_index"]) for row in rows) != tuple(range(9)):
            return Rejected("manifest_queue_mismatch", "manifest must account for queue indexes 0 through 8 in order")
        full_manifest: list[dict[str, object]] = []
        for index, row in enumerate(rows):
            if set(row) != {"queue_index", "disposition", "text", "source_ref"}:
                return Rejected("invalid_manifest", "manifest entries require only queue_index, disposition, text and source_ref")
            if row["disposition"] not in ("open", "constraint") or not _text(row["text"]).strip() or not _text(row["source_ref"]).strip():
                return Rejected("invalid_manifest", "every entry needs a reviewed disposition, nonempty text and source reference")
            full_manifest.append(row | {"queue_item_sha256": digest(queue[index].encode())})
        if not any(row["disposition"] == "open" for row in full_manifest):
            return Rejected("invalid_manifest", "reviewed manifest must identify open work")
        constraint_rows: list[object] = []
        authoring = [manifest_image]
        if constraints is not None:
            constraint_image = FileImage.capture(constraints)
            snapshots.append(constraint_image)
            authoring.append(constraint_image)
            constraint_rows = _array(cast(object, json.loads(decoded(constraint_image.data_b64))))
        sources = tuple(sorted({*implementation_sources(), configuration_source(), *bound_files}))
        source_images = tuple(FileImage.capture(path) for path in sources)
        snapshots.extend(source_images)
        package_sha256 = installed_package_digest()
        if isinstance(package_sha256, Rejected):
            return package_sha256
        bound_constraints: list[HumanConstraint] = []
        for row in constraint_rows:
            item = _object(row)
            if set(item) != {"source", "quote"}:
                return Rejected("invalid_constraint", "constraint must contain its source path and exact quote")
            source_image = FileImage.capture(Path(_text(item["source"])))
            snapshots.append(source_image)
            if not _text(item["quote"]) or _text(item["quote"]).encode() not in decoded(source_image.data_b64):
                return Rejected("constraint_quote_mismatch", "each Human constraint must quote its exact source bytes")
            bound_constraints.append(HumanConstraint(source_image.pin, _text(item["quote"])))
        inputs = Preparation(
            authority,
            task,
            todo,
            workspace,
            sources,
            package_sha256,
            tuple(
                ManifestItem(
                    _integer(item["queue_index"]), _text(item["queue_item_sha256"]), cast(Literal["open", "constraint"], _text(item["disposition"])), _text(item["text"]), _text(item["source_ref"])
                )
                for item in full_manifest
            ),
            tuple(bound_constraints),
            PreparationSnapshot(authority_image, task_image, todo_image, workspace_pin, tuple(image.pin for image in source_images), encoded(raw_queue(task_bytes)), tuple(authoring)),
        )
        payload = inputs.serialize()
        loaded = load_preparation(payload)
        if isinstance(loaded, Rejected):
            return loaded
        for constraint in loaded.constraints:
            source_path = Path(constraint.source.path)
            if not source_path.is_absolute() or source_path.resolve(strict=True) != source_path:
                return Rejected("noncanonical_constraint_source", "constraint sources must be canonical absolute paths")
            if not constraint.quote or constraint.quote.encode() not in _read_pin(constraint.source).data:
                return Rejected("constraint_quote_mismatch", "each Human constraint must quote its exact source bytes")
        for snapshot in snapshots:
            _ = _read_pin(snapshot.pin)
        if WorkspacePin.capture(workspace) != workspace_pin:
            return Rejected("workspace_changed", "workspace identity changed while authoring preparation")
        published = create_snapshot(output, payload, 0o600)
        return {"action": "build-preparation", "output": str(output), "preparation_sha256": digest(published.data), "queue_count": len(queue), "source_count": len(sources)}
    except (OSError, ValueError, KeyError, ReplaceError, TaskFrontmatterError) as exc:
        return Rejected("preparation_build_failed", str(exc))


def _same_preparation(packet: ExactPacket, inputs: Preparation) -> bool:
    return (
        (packet.authority.pin.path, packet.task.pin.path, packet.todo.pin.path, packet.workspace.path) == tuple(str(path) for path in (inputs.authority, inputs.task, inputs.todo, inputs.workspace))
        and tuple(pin.path for pin in packet.sources) == tuple(str(path) for path in inputs.sources)
        and packet.package_sha256 == inputs.package_sha256
        and packet.manifest == inputs.manifest
        and packet.constraints == inputs.constraints
        and inputs.snapshot is not None
        and packet.authority == inputs.snapshot.authority
        and packet.task == inputs.snapshot.task
        and packet.todo == inputs.snapshot.todo
        and packet.workspace == inputs.snapshot.workspace
        and packet.sources == inputs.snapshot.sources
        and packet.queue_b64 == inputs.snapshot.queue_b64
    )


def dispatch_control(
    action: str,
    preparation_path: Path,
    packet_path: Path,
    approval_path: Path,
    preparation_sha256: str,
    packet_sha256: str,
    approval_sha256: str,
    store: FenceStore,
    backend: ReplacementBackend,
) -> dict[str, object] | Rejected:
    """Serve only four exact Source1957 actions using server-owned artifact paths."""
    if action not in {"prepare", "inspect", "execute", "reconcile"}:
        return Rejected("unsupported_action", action)
    try:
        for path in (preparation_path, packet_path, approval_path):
            if path.exists() and stat.S_IMODE(path.lstat().st_mode) != 0o600:
                return Rejected("unsafe_control_artifact", "control artifacts must be private mode 0600")
        if action == "prepare":
            source = read_snapshot(preparation_path, "preparation inputs")
            if digest(source.data) != preparation_sha256 or packet_sha256 or approval_sha256:
                return Rejected("control_digest_mismatch", "prepare requires only its exact input digest")
            inputs = load_preparation(source.data)
            if isinstance(inputs, Rejected):
                return inputs
            previous = store.for_old_session(OLD_SESSION_ID)
            if previous is not None:
                saved = previous.receipts.get("exact_packet", {}).get("packet_b64")
                if not isinstance(saved, str):
                    return Rejected("reserved_packet_unavailable", previous.spec.operation_id)
                packet = load_packet(decoded(saved))
                if isinstance(packet, Rejected):
                    return packet
                if not _same_preparation(packet, inputs):
                    return Rejected("prepared_inputs_changed", "an existing reservation binds different inputs")
                invalid = validate(packet)
                if invalid:
                    return invalid
            else:
                packet = prepare(inputs, store, backend)
                if isinstance(packet, Rejected):
                    return packet
            if packet_path.exists():
                if read_snapshot(packet_path, "prepared packet").data != packet.serialize():
                    return Rejected("packet_output_conflict", "packet output already contains different bytes")
            else:
                _ = create_snapshot(packet_path, packet.serialize(), 0o600)
        else:
            raw = read_snapshot(packet_path, "exact packet").data
            if preparation_sha256 or digest(raw) != packet_sha256:
                return Rejected("control_digest_mismatch", "operation requires the exact stored packet digest")
            packet = load_packet(raw)
            if isinstance(packet, Rejected):
                return packet
        if action in {"prepare", "inspect"}:
            if approval_sha256:
                return Rejected("control_digest_mismatch", "review digest is only used for execute or reconcile")
            operation = inspect(packet, store)
        else:
            review = read_snapshot(approval_path, "separate packet approval")
            if digest(review.data) != approval_sha256:
                return Rejected("control_digest_mismatch", "review artifact changed")
            approval = ReviewApproval(packet.sha256, FilePin.capture(review))
            operation = execute(packet, approval, store, backend) if action == "execute" else reconcile(packet, approval, store, backend)
        if isinstance(operation, Rejected):
            return operation
        return {"action": action, "operation_id": packet.operation_id, "packet_sha256": packet.sha256, "phase": operation.phase, "status": operation.status, "successor_id": packet.successor_id}
    except (OSError, ValueError, ReplaceError) as exc:
        return Rejected("control_failed", str(exc))


def control_request(socket_path: Path, request: dict[str, str], *, timeout_s: float = 180) -> dict[str, object] | Rejected:
    """Send digest-only requests to an owned private local control socket."""
    try:
        info = socket_path.lstat()
        parent = socket_path.parent.stat()
        if (
            socket_path.resolve(strict=True) != socket_path
            or not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            return Rejected("unsafe_control_socket", "control socket must be canonical, owned and mode 0600")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_s)
            connection.connect(str(socket_path))
            _pid, peer_uid, _gid = cast(tuple[int, int, int], struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))))
            if peer_uid != os.getuid():
                return Rejected("wrong_control_peer", "control server belongs to another user")
            connection.sendall(canonical(request).encode() + b"\n")
            raw = bytearray()
            while not raw.endswith(b"\n"):
                chunk = connection.recv(8193 - len(raw))
                if not chunk or len(raw) + len(chunk) > 8192:
                    return Rejected("control_response_lost", "inspect the durable operation before any retry")
                raw.extend(chunk)
            return _object(cast(object, json.loads(raw)))
    except (OSError, ValueError) as exc:
        return Rejected("control_unavailable", str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    builder = commands.add_parser("build-preparation", help="write private preparation inputs only; no reservation or lifecycle action")
    for name in ("authority", "task", "todo", "workspace"):
        _ = builder.add_argument(f"--{name}", required=True)
    _ = builder.add_argument("--manifest", required=True, help="JSON array of {queue_index, disposition, text, source_ref}; indexes 0..8 in order; disposition is open or constraint")
    _ = builder.add_argument("--output", required=True, help="new canonical artifact path in an existing owned 0700 directory outside work logs; writes mode 0600 without overwrite")
    _ = builder.add_argument("--constraints", help="optional JSON array of exact {source, quote} objects")
    _ = builder.add_argument("--bound-file", action="append", default=[], help="repeat for the exact backend configuration file, its bridge_state_path and its host_config_path")
    for action in ("prepare", "inspect", "execute", "reconcile"):
        command = commands.add_parser(action)
        _ = command.add_argument("--control-socket", required=True)
        _ = command.add_argument("--preparation-sha256", default="")
        _ = command.add_argument("--packet-sha256", default="")
        _ = command.add_argument("--approval-sha256", default="")
    args = cast(dict[str, object], vars(parser.parse_args(argv)))
    if args["action"] == "build-preparation":
        result = build_preparation(
            authority=Path(_text(args["authority"])),
            task=Path(_text(args["task"])),
            todo=Path(_text(args["todo"])),
            workspace=Path(_text(args["workspace"])),
            manifest=Path(_text(args["manifest"])),
            output=Path(_text(args["output"])),
            constraints=Path(_text(args["constraints"])) if args["constraints"] is not None else None,
            bound_files=tuple(Path(_text(path)) for path in _array(args["bound_file"])),
        )
    else:
        result = control_request(
            Path(_text(args["control_socket"])),
            {
                "action": _text(args["action"]),
                "preparation_sha256": _text(args["preparation_sha256"]),
                "packet_sha256": _text(args["packet_sha256"]),
                "approval_sha256": _text(args["approval_sha256"]),
            },
        )
    print(canonical(asdict(result) if isinstance(result, Rejected) else result))
    return 1 if isinstance(result, Rejected) or "code" in result or result.get("ok") is False else 0


if __name__ == "__main__":
    sys.exit(main())
