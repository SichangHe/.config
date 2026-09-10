#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Move one complete ordered task queue through a recoverable manifest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_manager_replace import ReplaceError, Snapshot, create_snapshot, read_snapshot, replace_snapshot, task_path
from omo_manager.omo_task_edit import has_live_pending_marker, render_pending_items
from omo_manager.omo_task_lock import canonical_target as canonical_lock_target
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V1, TaskFrontmatterError, TaskMetadata, parse_task_metadata, runat_kind

REQUEST_SCHEMA = "omo-queue-transfer-request/v1"
MANIFEST_SCHEMA = "omo-queue-transfer-manifest/v1"
RECEIPT_SCHEMA = "omo-queue-transfer-receiver/v1"
TERMINAL_KINDS = frozenset({"cancelled", "completed", "duplicate"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RECORD_BYTES = 32 * 1024 * 1024
LOCK_TIMEOUT_S = 5.0


class QueueTransferError(RuntimeError):
    """The queue transfer could not be authenticated or completed."""


@dataclass(frozen=True)
class DestinationPin:
    task: str
    task_sha256: str


@dataclass(frozen=True)
class Disposition:
    source_index: int
    kind: str
    destination: str
    evidence: str


@dataclass(frozen=True)
class Request:
    source_task: str
    source_task_sha256: str
    source_queue: tuple[str, ...]
    source_queue_sha256: str
    destinations: tuple[DestinationPin, ...]
    dispositions: tuple[Disposition, ...]


@dataclass(frozen=True)
class FilePlan:
    task: str
    before: bytes
    after: bytes
    before_queue: tuple[str, ...]
    after_queue: tuple[str, ...]


@dataclass(frozen=True)
class DestinationPlan:
    file: FilePlan
    transferred: tuple[str, ...]
    receipt_path: Path


@dataclass(frozen=True)
class Plan:
    root: Path
    transaction_id: str
    request_sha256: str
    request_data: bytes
    source: FilePlan
    destinations: tuple[DestinationPlan, ...]
    dispositions: tuple[Disposition, ...]


@dataclass(frozen=True)
class PrepareArgs:
    root: Path
    request: Path
    request_sha256: str
    manifest: Path


@dataclass(frozen=True)
class BoundArgs:
    root: Path
    manifest: Path
    manifest_sha256: str


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def queue_digest(items: tuple[str, ...]) -> str:
    return digest("\0".join(items).encode())


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def encoded(data: bytes) -> str:
    return base64.b64encode(data).decode()


def decoded(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise QueueTransferError(f"{label} must be base64 text")
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise QueueTransferError(f"{label} is not canonical base64") from exc
    if encoded(data) != value:
        raise QueueTransferError(f"{label} is not canonical base64")
    return data


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QueueTransferError(f"JSON contains duplicate key {key!r}")
        result[key] = value
    return result


def object_from_bytes(data: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(data, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueueTransferError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise QueueTransferError(f"{label} must contain one JSON object")
    return value


def exact_keys(value: dict[str, object], expected: set[str], label: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        detail = f"missing {sorted(missing)[0]!r}" if missing else f"unknown {sorted(extra)[0]!r}"
        raise QueueTransferError(f"{label} has {detail}")


def text_field(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        requirement = "text" if allow_empty else "nonempty text"
        raise QueueTransferError(f"{label} must be {requirement}")
    return value


def sha256_field(value: object, label: str) -> str:
    text = text_field(value, label)
    if SHA256_RE.fullmatch(text) is None:
        raise QueueTransferError(f"{label} must be one lowercase SHA-256 digest")
    return text


def list_field(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise QueueTransferError(f"{label} must be a list")
    return value


def task_ref(value: object, label: str) -> str:
    ref = text_field(value, label)
    if Path(ref).is_absolute() or not ref.endswith(".md"):
        raise QueueTransferError(f"{label} must be a relative Markdown task reference")
    return ref


def queue_items(value: object, label: str) -> tuple[str, ...]:
    values = list_field(value, label)
    if not all(isinstance(item, str) and item and "\n" not in item and "\r" not in item for item in values):
        raise QueueTransferError(f"{label} must contain nonempty one-line strings")
    return tuple(item for item in values if isinstance(item, str))


def parse_destination_pin(value: object, index: int) -> DestinationPin:
    if not isinstance(value, dict):
        raise QueueTransferError(f"destinations[{index}] must be an object")
    exact_keys(value, {"task", "task_sha256"}, f"destinations[{index}]")
    return DestinationPin(task_ref(value["task"], f"destinations[{index}].task"), sha256_field(value["task_sha256"], f"destinations[{index}].task_sha256"))


def parse_disposition(value: object, index: int) -> Disposition:
    if not isinstance(value, dict):
        raise QueueTransferError(f"dispositions[{index}] must be an object")
    exact_keys(value, {"source_index", "kind", "destination", "evidence"}, f"dispositions[{index}]")
    source_index = value["source_index"]
    if not isinstance(source_index, int) or isinstance(source_index, bool) or source_index != index:
        raise QueueTransferError("dispositions must enumerate the ordered source queue exactly once")
    kind = text_field(value["kind"], f"dispositions[{index}].kind")
    destination = text_field(value["destination"], f"dispositions[{index}].destination", allow_empty=True)
    evidence = text_field(value["evidence"], f"dispositions[{index}].evidence", allow_empty=True)
    if "\n" in evidence or "\r" in evidence:
        raise QueueTransferError("disposition evidence must be one line")
    if kind == "transfer":
        if not destination or evidence:
            raise QueueTransferError("transfer dispositions require one destination and no terminal evidence")
    elif kind in TERMINAL_KINDS:
        if destination or not evidence.strip() or evidence.strip() == "(pending)":
            raise QueueTransferError("terminal dispositions require one-line evidence and no destination")
        if any(marker in evidence.casefold() for marker in ("<human_instruction", "</human_instruction>", "<manager_delegation", "</manager_delegation>")):
            raise QueueTransferError("terminal disposition evidence contains a reserved authority envelope")
    else:
        raise QueueTransferError(f"unsupported disposition kind {kind!r}")
    return Disposition(source_index, kind, destination, evidence)


def parse_request(data: bytes) -> Request:
    if len(data) > MAX_RECORD_BYTES:
        raise QueueTransferError("queue-transfer request exceeds the size bound")
    value = object_from_bytes(data, "queue-transfer request")
    exact_keys(
        value,
        {"schema", "source_task", "source_task_sha256", "source_queue", "source_queue_sha256", "destinations", "dispositions"},
        "queue-transfer request",
    )
    if value["schema"] != REQUEST_SCHEMA:
        raise QueueTransferError("queue-transfer request schema is invalid")
    source_queue = queue_items(value["source_queue"], "source_queue")
    if len(source_queue) < 2:
        raise QueueTransferError("a multi-item transfer requires at least two source items")
    if queue_digest(source_queue) != sha256_field(value["source_queue_sha256"], "source_queue_sha256"):
        raise QueueTransferError("source queue digest does not match its ordered items")
    destinations = tuple(parse_destination_pin(item, index) for index, item in enumerate(list_field(value["destinations"], "destinations")))
    if not destinations or len({item.task for item in destinations}) != len(destinations):
        raise QueueTransferError("destinations must name at least one distinct task")
    dispositions = tuple(parse_disposition(item, index) for index, item in enumerate(list_field(value["dispositions"], "dispositions")))
    if len(dispositions) != len(source_queue):
        raise QueueTransferError("every source item must have exactly one ordered disposition")
    named = {item.task for item in destinations}
    used = {item.destination for item in dispositions if item.kind == "transfer"}
    if named != used:
        raise QueueTransferError("destination pins must exactly match transfer dispositions")
    transferred_items = tuple(source_queue[item.source_index] for item in dispositions if item.kind == "transfer")
    if len(set(transferred_items)) != len(transferred_items):
        raise QueueTransferError("duplicate source text may transfer once; disposition other copies as duplicates")
    return Request(
        task_ref(value["source_task"], "source_task"),
        sha256_field(value["source_task_sha256"], "source_task_sha256"),
        source_queue,
        sha256_field(value["source_queue_sha256"], "source_queue_sha256"),
        destinations,
        dispositions,
    )


def metadata(data: bytes, root: Path, label: str) -> TaskMetadata:
    try:
        value = parse_task_metadata(data.decode(), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise QueueTransferError(f"{label} has invalid task frontmatter: {exc}") from exc
    if value is None or value.version != TASK_FRONTMATTER_V1:
        raise QueueTransferError(f"{label} must have v1 task frontmatter")
    return value


def same_identity(before: TaskMetadata, after: TaskMetadata) -> bool:
    return (
        before.version,
        before.status,
        before.blocked_on,
        before.runat,
        before.tool,
        before.managerat,
        before.is_manager,
        before.session_id,
    ) == (
        after.version,
        after.status,
        after.blocked_on,
        after.runat,
        after.tool,
        after.managerat,
        after.is_manager,
        after.session_id,
    )


def receipt_name(transaction_id: str, task: str) -> str:
    task_key = digest(task.encode())[:16]
    return f".omo-queue-transfer-{transaction_id}.{task_key}.receipt.json"


def source_after(data: bytes, root: Path, transaction_id: str, queue: tuple[str, ...], dispositions: tuple[Disposition, ...]) -> bytes:
    text = data.decode()
    updated = render_pending_items(text, ())
    rows: list[str] = []
    for disposition, item in zip(dispositions, queue, strict=True):
        outcome = f"transferred to {disposition.destination}" if disposition.kind == "transfer" else f"{disposition.kind}: {disposition.evidence}"
        rows.append(f"(queue transfer {transaction_id} item {disposition.source_index + 1}/{len(queue)} {digest(item.encode())}: {outcome})")
    updated = updated.rstrip("\n") + "\n" + "\n".join(rows) + "\n"
    before_metadata = metadata(data, root, "source task")
    after_metadata = metadata(updated.encode(), root, "updated source task")
    if after_metadata.pending_task_items or not same_identity(before_metadata, after_metadata):
        raise QueueTransferError("source construction changed lifecycle identity")
    return updated.encode()


def destination_after(
    data: bytes,
    root: Path,
    source_queue: tuple[str, ...],
    transferred: tuple[str, ...],
    label: str,
) -> tuple[bytes, tuple[str, ...]]:
    before_metadata = metadata(data, root, label)
    if before_metadata.status == "done":
        raise QueueTransferError(f"{label} is done")
    if set(source_queue) & set(before_metadata.pending_task_items):
        raise QueueTransferError(f"{label} already contains a source item")
    after_queue = (*before_metadata.pending_task_items, *transferred)
    updated = render_pending_items(data.decode(), after_queue).encode()
    after_metadata = metadata(updated, root, f"updated {label}")
    if after_metadata.pending_task_items != after_queue or not same_identity(before_metadata, after_metadata):
        raise QueueTransferError(f"{label} construction changed lifecycle identity")
    return updated, after_queue


def receipt_record(
    plan: Plan,
    destination: DestinationPlan,
) -> dict[str, object]:
    file = destination.file
    transferred = destination.transferred
    return {
        "schema": RECEIPT_SCHEMA,
        "transaction_id": plan.transaction_id,
        "request_sha256": plan.request_sha256,
        "committed_manifest_sha256": digest(manifest_bytes(plan, "committed")),
        "source_task": plan.source.task,
        "source_queue_count": len(plan.source.before_queue),
        "source_queue_sha256": queue_digest(plan.source.before_queue),
        "destination_task": file.task,
        "before_task_sha256": digest(file.before),
        "after_task_sha256": digest(file.after),
        "before_queue_count": len(file.before_queue),
        "before_queue_sha256": queue_digest(file.before_queue),
        "transferred_count": len(transferred),
        "transferred_queue_sha256": queue_digest(transferred),
        "transferred_items": list(transferred),
        "after_queue_count": len(file.after_queue),
        "after_queue_sha256": queue_digest(file.after_queue),
    }


def build_plan(
    root: Path,
    transaction_id: str,
    request_sha256: str,
    request_data: bytes,
    source_task: str,
    source_data: bytes,
    destination_data: tuple[tuple[str, bytes], ...],
    dispositions: tuple[Disposition, ...],
) -> Plan:
    source_metadata = metadata(source_data, root, "source task")
    if source_metadata.status == "done" or not source_metadata.pending_task_items:
        raise QueueTransferError("source task must be active with a nonempty queue")
    source_queue = source_metadata.pending_task_items
    if len(source_queue) < 2:
        raise QueueTransferError("source queue must contain at least two items")
    if len(dispositions) != len(source_queue) or tuple(item.source_index for item in dispositions) != tuple(range(len(source_queue))):
        raise QueueTransferError("dispositions do not bind every ordered source item")
    transferred_items = tuple(source_queue[item.source_index] for item in dispositions if item.kind == "transfer")
    if len(set(transferred_items)) != len(transferred_items):
        raise QueueTransferError("duplicate source text may transfer once; disposition other copies as duplicates")
    if has_live_pending_marker(source_data.decode()):
        raise QueueTransferError("source task has a live pending-delivery marker")
    source_file = FilePlan(source_task, source_data, source_after(source_data, root, transaction_id, source_queue, dispositions), source_queue, ())
    destination_by_task = dict(destination_data)
    transfer_tasks = {item.destination for item in dispositions if item.kind == "transfer"}
    if set(destination_by_task) != transfer_tasks or source_task in destination_by_task:
        raise QueueTransferError("manifest destination set does not exactly match its transfer dispositions")
    destinations: list[DestinationPlan] = []
    for destination_task in sorted(destination_by_task):
        before = destination_by_task[destination_task]
        if has_live_pending_marker(before.decode()):
            raise QueueTransferError(f"destination task {destination_task} has a live pending-delivery marker")
        transferred = tuple(source_queue[item.source_index] for item in dispositions if item.kind == "transfer" and item.destination == destination_task)
        after, after_queue = destination_after(before, root, source_queue, transferred, f"destination task {destination_task}")
        before_queue = metadata(before, root, f"destination task {destination_task}").pending_task_items
        file = FilePlan(destination_task, before, after, before_queue, after_queue)
        receipt_path = root / receipt_name(transaction_id, destination_task)
        destinations.append(DestinationPlan(file, transferred, receipt_path))
    return Plan(root, transaction_id, request_sha256, request_data, source_file, tuple(destinations), dispositions)


def disposition_record(plan: Plan, disposition: Disposition) -> dict[str, object]:
    item = plan.source.before_queue[disposition.source_index]
    return {
        "source_index": disposition.source_index,
        "item_sha256": digest(item.encode()),
        "kind": disposition.kind,
        "destination": disposition.destination,
        "evidence": disposition.evidence,
    }


def file_record(file: FilePlan) -> dict[str, object]:
    return {
        "task": file.task,
        "before_task_sha256": digest(file.before),
        "after_task_sha256": digest(file.after),
        "before_data": encoded(file.before),
        "after_data": encoded(file.after),
        "before_queue_count": len(file.before_queue),
        "before_queue_sha256": queue_digest(file.before_queue),
        "after_queue_count": len(file.after_queue),
        "after_queue_sha256": queue_digest(file.after_queue),
    }


def manifest_record(plan: Plan, phase: str) -> dict[str, object]:
    source = {**file_record(plan.source), "ordered_queue": list(plan.source.before_queue)}
    destinations = []
    for destination in plan.destinations:
        destinations.append(
            {
                **file_record(destination.file),
                "transferred_count": len(destination.transferred),
                "transferred_queue_sha256": queue_digest(destination.transferred),
                "transferred_items": list(destination.transferred),
                "receiver_receipt": destination.receipt_path.name,
            }
        )
    terminal_count = sum(item.kind != "transfer" for item in plan.dispositions)
    record: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "phase": phase,
        "transaction_id": plan.transaction_id,
        "request_sha256": plan.request_sha256,
        "request_data": encoded(plan.request_data),
        "root": str(plan.root),
        "source": source,
        "dispositions": [disposition_record(plan, item) for item in plan.dispositions],
        "destinations": destinations,
        "proof": {
            "source_count": len(plan.source.before_queue),
            "disposition_count": len(plan.dispositions),
            "transferred_count": len(plan.dispositions) - terminal_count,
            "terminal_count": terminal_count,
            "ordered_source_sha256": queue_digest(plan.source.before_queue),
        },
    }
    commitment_payload = {key: value for key, value in record.items() if key != "phase"}
    record["commitment_sha256"] = digest(canonical_bytes(commitment_payload))
    data = canonical_bytes(record)
    if len(data) > MAX_RECORD_BYTES:
        raise QueueTransferError("queue-transfer manifest exceeds the size bound")
    return record


def manifest_bytes(plan: Plan, phase: str) -> bytes:
    return canonical_bytes(manifest_record(plan, phase))


def disposition_from_manifest(value: object, index: int) -> Disposition:
    if not isinstance(value, dict):
        raise QueueTransferError(f"manifest disposition {index} is not an object")
    exact_keys(value, {"source_index", "item_sha256", "kind", "destination", "evidence"}, f"manifest disposition {index}")
    disposition = parse_disposition(
        {key: value[key] for key in ("source_index", "kind", "destination", "evidence")},
        index,
    )
    _ = sha256_field(value["item_sha256"], f"manifest disposition {index} item digest")
    return disposition


def plan_from_manifest(data: bytes, expected_root: Path) -> tuple[Plan, str]:
    if len(data) > MAX_RECORD_BYTES:
        raise QueueTransferError("queue-transfer manifest exceeds the size bound")
    value = object_from_bytes(data, "queue-transfer manifest")
    exact_keys(
        value,
        {
            "schema",
            "phase",
            "transaction_id",
            "request_sha256",
            "request_data",
            "root",
            "source",
            "dispositions",
            "destinations",
            "proof",
            "commitment_sha256",
        },
        "queue-transfer manifest",
    )
    if value["schema"] != MANIFEST_SCHEMA or value["phase"] not in {"prepared", "committed"}:
        raise QueueTransferError("queue-transfer manifest schema or phase is invalid")
    root = Path(text_field(value["root"], "manifest root"))
    if root != expected_root:
        raise QueueTransferError("queue-transfer manifest root does not match the invocation")
    transaction_id = sha256_field(value["transaction_id"], "transaction_id")
    request_sha256 = sha256_field(value["request_sha256"], "request_sha256")
    if transaction_id != request_sha256:
        raise QueueTransferError("transaction id does not match the bound request")
    request_data = decoded(value["request_data"], "manifest request_data")
    if digest(request_data) != request_sha256:
        raise QueueTransferError("manifest request bytes do not match the bound request digest")
    request = parse_request(request_data)
    source_value = value["source"]
    if not isinstance(source_value, dict):
        raise QueueTransferError("manifest source must be an object")
    source_task = task_ref(source_value.get("task"), "manifest source task")
    source_data = decoded(source_value.get("before_data"), "manifest source before_data")
    destination_data: list[tuple[str, bytes]] = []
    for index, destination_value in enumerate(list_field(value["destinations"], "manifest destinations")):
        if not isinstance(destination_value, dict):
            raise QueueTransferError(f"manifest destination {index} must be an object")
        destination_data.append(
            (
                task_ref(destination_value.get("task"), f"manifest destination {index} task"),
                decoded(destination_value.get("before_data"), f"manifest destination {index} before_data"),
            )
        )
    dispositions = tuple(disposition_from_manifest(item, index) for index, item in enumerate(list_field(value["dispositions"], "manifest dispositions")))
    destination_sha256 = {task: digest(data) for task, data in destination_data}
    if (
        request.source_task != source_task
        or request.source_task_sha256 != digest(source_data)
        or request.dispositions != dispositions
        or {item.task: item.task_sha256 for item in request.destinations} != destination_sha256
    ):
        raise QueueTransferError("manifest task snapshots or dispositions do not match the bound request")
    plan = build_plan(root, transaction_id, request_sha256, request_data, source_task, source_data, tuple(destination_data), dispositions)
    if plan.source.before_queue != request.source_queue or queue_digest(plan.source.before_queue) != request.source_queue_sha256:
        raise QueueTransferError("manifest source queue does not match the bound request")
    expected = manifest_bytes(plan, text_field(value["phase"], "manifest phase"))
    if data != expected:
        raise QueueTransferError("queue-transfer manifest is not its canonical reconstruction")
    return plan, text_field(value["phase"], "manifest phase")


def root_manifest_path(root: Path, transaction_id: str) -> Path:
    return root / f".omo-queue-transfer-{transaction_id}.json"


def require_bound_path(root: Path, path: Path, expected: Path, label: str) -> None:
    resolved = path.expanduser().resolve(strict=False)
    if resolved != expected or resolved.parent != root:
        raise QueueTransferError(f"{label} must be the exact transaction-bound direct child of the work-log root")


def task_targets(root: Path, snapshots: tuple[tuple[str, bytes], ...]) -> tuple[str, ...]:
    targets = tuple(metadata(data, root, label).runat for label, data in snapshots)
    canonical = tuple(canonical_lock_target(target) for target in targets)
    if len(set(canonical)) != len(canonical):
        raise QueueTransferError("source and destination tasks must have distinct owner targets")
    if any(runat_kind(target) == "tmux" and target.partition(":")[0].startswith("h") for target in targets):
        raise QueueTransferError("queue transfer does not mutate human-owned tmux targets")
    return targets


def plan_targets(plan: Plan) -> tuple[str, ...]:
    snapshots = (("source task", plan.source.before),) + tuple(
        (f"destination task {destination.file.task}", destination.file.before) for destination in plan.destinations
    )
    return task_targets(plan.root, snapshots)


def lock_paths(root: Path, paths: tuple[Path, ...], targets: tuple[str, ...] = ()) -> ExitStack:
    stack = ExitStack()
    try:
        stack.enter_context(task_file_lock(root / ".omo-task-membership.lock", timeout_s=LOCK_TIMEOUT_S))
        for target in sorted({canonical_lock_target(item) for item in targets}):
            stack.enter_context(task_target_lock(root, target, timeout_s=LOCK_TIMEOUT_S))
        for path in sorted(set(paths), key=str):
            stack.enter_context(task_file_lock(path, timeout_s=LOCK_TIMEOUT_S))
    except Exception:
        stack.close()
        raise
    return stack


def read_exact(path: Path, expected_sha256: str, label: str) -> Snapshot:
    snapshot = read_snapshot(path, label)
    if digest(snapshot.data) != expected_sha256:
        raise QueueTransferError(f"{label} digest changed")
    return snapshot


def require_private(snapshot: Snapshot, label: str) -> None:
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600:
        raise QueueTransferError(f"{label} must be owner-private mode 0600")


def read_recoverable_manifest(path: Path, expected_prepared_sha256: str, root: Path) -> tuple[Snapshot, Plan, str]:
    snapshot = read_snapshot(path, "queue-transfer manifest")
    require_private(snapshot, "queue-transfer manifest")
    plan, phase = plan_from_manifest(snapshot.data, root)
    if digest(snapshot.data) != expected_prepared_sha256 and not (phase == "committed" and digest(manifest_bytes(plan, "prepared")) == expected_prepared_sha256):
        raise QueueTransferError("queue-transfer manifest digest changed outside its bound commit transition")
    return snapshot, plan, phase


def request_task_paths(root: Path, request: Request) -> tuple[Path, ...]:
    paths = (task_path(root, request.source_task), *(task_path(root, item.task) for item in request.destinations))
    if len(set(paths)) != len(paths):
        raise QueueTransferError("source and destination task paths must be distinct")
    return paths


def prepare_transfer(args: PrepareArgs) -> Snapshot:
    root = args.root.expanduser().resolve(strict=True)
    if v2_enabled(root):
        raise QueueTransferError("v1 queue transfer is disabled after v2 enablement")
    if SHA256_RE.fullmatch(args.request_sha256) is None:
        raise QueueTransferError("request digest must be one lowercase SHA-256 digest")
    request_path = args.request.expanduser().resolve(strict=True)
    request_snapshot = read_exact(request_path, args.request_sha256, "queue-transfer request")
    require_private(request_snapshot, "queue-transfer request")
    request = parse_request(request_snapshot.data)
    transaction_id = args.request_sha256
    expected_manifest = root_manifest_path(root, transaction_id)
    require_bound_path(root, args.manifest, expected_manifest, "manifest")
    task_paths = request_task_paths(root, request)
    task_snapshots = tuple(read_snapshot(path, f"task preflight {path.name}") for path in task_paths)
    targets = task_targets(root, tuple((f"task preflight {path.name}", snapshot.data) for path, snapshot in zip(task_paths, task_snapshots, strict=True)))
    with lock_paths(root, (request_path, expected_manifest, *task_paths), targets):
        request_snapshot = read_exact(request_path, args.request_sha256, "queue-transfer request")
        require_private(request_snapshot, "queue-transfer request")
        request = parse_request(request_snapshot.data)
        task_paths = request_task_paths(root, request)
        source = read_exact(task_paths[0], request.source_task_sha256, "source task")
        destination_snapshots = tuple(read_exact(path, pin.task_sha256, f"destination task {pin.task}") for path, pin in zip(task_paths[1:], request.destinations, strict=True))
        plan = build_plan(
            root,
            transaction_id,
            args.request_sha256,
            request_snapshot.data,
            request.source_task,
            source.data,
            tuple((pin.task, snapshot.data) for pin, snapshot in zip(request.destinations, destination_snapshots, strict=True)),
            request.dispositions,
        )
        if plan.source.before_queue != request.source_queue or queue_digest(plan.source.before_queue) != request.source_queue_sha256:
            raise QueueTransferError("live source queue does not match the ordered request snapshot")
        return create_snapshot(expected_manifest, manifest_bytes(plan, "prepared"), 0o600)


def current_state(plan: Plan) -> tuple[Snapshot, tuple[Snapshot, ...]]:
    source = read_snapshot(task_path(plan.root, plan.source.task), "current source task")
    destinations = tuple(read_snapshot(task_path(plan.root, item.file.task), f"current destination task {item.file.task}") for item in plan.destinations)
    if source.data not in {plan.source.before, plan.source.after}:
        raise QueueTransferError("source task has unknown bytes during transaction recovery")
    for snapshot, destination in zip(destinations, plan.destinations, strict=True):
        if snapshot.data not in {destination.file.before, destination.file.after}:
            raise QueueTransferError(f"destination task {destination.file.task} has unknown bytes during transaction recovery")
    if source.data == plan.source.before and any(snapshot.data == destination.file.after for snapshot, destination in zip(destinations, plan.destinations, strict=True)):
        raise QueueTransferError("destination acquired work before the source relinquished ownership")
    return source, destinations


def receipt_data(plan: Plan, destination: DestinationPlan) -> bytes:
    return canonical_bytes(receipt_record(plan, destination))


def ensure_receipt(plan: Plan, destination: DestinationPlan) -> Snapshot:
    data = receipt_data(plan, destination)
    if destination.receipt_path.exists() or destination.receipt_path.is_symlink():
        return read_exact(destination.receipt_path, digest(data), f"receiver receipt {destination.file.task}")
    return create_snapshot(destination.receipt_path, data, 0o600)


def prove_receipts(plan: Plan) -> None:
    for destination in plan.destinations:
        data = receipt_data(plan, destination)
        receipt = read_exact(destination.receipt_path, digest(data), f"receiver receipt {destination.file.task}")
        if receipt.data != data or stat.S_IMODE(receipt.state.st_mode) != 0o600:
            raise QueueTransferError(f"receiver receipt {destination.file.task} is not exact and owner-private")


def prove_current_after(plan: Plan) -> None:
    source, destinations = current_state(plan)
    if source.data != plan.source.after:
        raise QueueTransferError("source queue was not cleared")
    for snapshot, destination in zip(destinations, plan.destinations, strict=True):
        if snapshot.data != destination.file.after:
            raise QueueTransferError(f"destination task {destination.file.task} did not reach its bound after-state")


def apply_transfer(args: BoundArgs) -> Snapshot:
    root = args.root.expanduser().resolve(strict=True)
    if v2_enabled(root):
        raise QueueTransferError("v1 queue transfer is disabled after v2 enablement")
    if SHA256_RE.fullmatch(args.manifest_sha256) is None:
        raise QueueTransferError("manifest digest must be one lowercase SHA-256 digest")
    manifest_path = args.manifest.expanduser().resolve(strict=True)
    _initial, plan, _phase = read_recoverable_manifest(manifest_path, args.manifest_sha256, root)
    require_bound_path(root, manifest_path, root_manifest_path(root, plan.transaction_id), "manifest")
    task_paths = (task_path(root, plan.source.task), *(task_path(root, item.file.task) for item in plan.destinations))
    receipt_paths = tuple(item.receipt_path for item in plan.destinations)
    with lock_paths(root, (manifest_path, *task_paths, *receipt_paths), plan_targets(plan)):
        manifest, plan, phase = read_recoverable_manifest(manifest_path, args.manifest_sha256, root)
        if phase == "committed":
            prove_current_after(plan)
            for destination in plan.destinations:
                _ = ensure_receipt(plan, destination)
            prove_receipts(plan)
            return manifest
        for path in receipt_paths:
            if path.exists() or path.is_symlink():
                raise QueueTransferError("receiver receipt exists before the transaction commit")
        source, destinations = current_state(plan)
        if source.data == plan.source.before:
            updated_source = replace_snapshot(source, plan.source.after, "source task")
            if updated_source.data != plan.source.after:
                raise QueueTransferError("source task replacement did not commit")
        for snapshot, destination in zip(destinations, plan.destinations, strict=True):
            if snapshot.data == destination.file.before:
                updated = replace_snapshot(snapshot, destination.file.after, f"destination task {destination.file.task}")
                if updated.data != destination.file.after:
                    raise QueueTransferError(f"destination task {destination.file.task} replacement did not commit")
        prove_current_after(plan)
        committed = replace_snapshot(manifest, manifest_bytes(plan, "committed"), "queue-transfer manifest")
        parsed, committed_phase = plan_from_manifest(committed.data, root)
        if committed_phase != "committed" or parsed != plan:
            raise QueueTransferError("committed manifest verification failed")
        for destination in plan.destinations:
            _ = ensure_receipt(plan, destination)
        prove_receipts(plan)
        return committed


def verify_transfer(args: BoundArgs) -> Plan:
    root = args.root.expanduser().resolve(strict=True)
    manifest_path = args.manifest.expanduser().resolve(strict=True)
    manifest = read_exact(manifest_path, args.manifest_sha256, "committed queue-transfer manifest")
    require_private(manifest, "committed queue-transfer manifest")
    plan, phase = plan_from_manifest(manifest.data, root)
    require_bound_path(root, manifest_path, root_manifest_path(root, plan.transaction_id), "manifest")
    if phase != "committed":
        raise QueueTransferError("queue-transfer manifest is not committed")
    task_paths = (task_path(root, plan.source.task), *(task_path(root, item.file.task) for item in plan.destinations))
    with lock_paths(root, (manifest_path, *task_paths, *(item.receipt_path for item in plan.destinations)), plan_targets(plan)):
        manifest = read_exact(manifest_path, args.manifest_sha256, "committed queue-transfer manifest")
        require_private(manifest, "committed queue-transfer manifest")
        plan, phase = plan_from_manifest(manifest.data, root)
        if phase != "committed":
            raise QueueTransferError("queue-transfer manifest is not committed")
        prove_current_after(plan)
        prove_receipts(plan)
    return plan


class ParsedArgs(argparse.Namespace):
    command: str
    root: Path
    request: Path
    request_sha256: str
    manifest: Path
    manifest_sha256: str


def parse_args(argv: list[str]) -> PrepareArgs | BoundArgs:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="Freeze an exact queue and all dispositions without editing tasks.")
    _ = prepare.add_argument("--root", type=Path, required=True)
    _ = prepare.add_argument("--request", type=Path, required=True)
    _ = prepare.add_argument("--request-sha256", required=True)
    _ = prepare.add_argument("--manifest", type=Path, required=True)
    for command in ("apply", "verify"):
        bound = subparsers.add_parser(command, help=f"{command.capitalize()} one exact prepared queue-transfer manifest.")
        _ = bound.add_argument("--root", type=Path, required=True)
        _ = bound.add_argument("--manifest", type=Path, required=True)
        _ = bound.add_argument("--manifest-sha256", required=True)
    parsed = parser.parse_args(argv, namespace=ParsedArgs())
    if parsed.command == "prepare":
        return PrepareArgs(parsed.root, parsed.request, parsed.request_sha256, parsed.manifest)
    return BoundArgs(parsed.root, parsed.manifest, parsed.manifest_sha256)


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    try:
        args = parse_args(values)
        command = values[0]
        if command == "prepare" and isinstance(args, PrepareArgs):
            manifest = prepare_transfer(args)
            plan, _phase = plan_from_manifest(manifest.data, args.root.expanduser().resolve(strict=True))
            print(
                f"prepared transaction={plan.transaction_id} manifest={manifest.path} manifest-sha256={digest(manifest.data)} "
                f"source-count={len(plan.source.before_queue)} destinations={len(plan.destinations)}"
            )
        elif command == "apply" and isinstance(args, BoundArgs):
            manifest = apply_transfer(args)
            plan, _phase = plan_from_manifest(manifest.data, args.root.expanduser().resolve(strict=True))
            print(f"committed transaction={plan.transaction_id} manifest-sha256={digest(manifest.data)}")
        elif command == "verify" and isinstance(args, BoundArgs):
            plan = verify_transfer(args)
            transferred = sum(item.kind == "transfer" for item in plan.dispositions)
            print(
                f"verified transaction={plan.transaction_id} source-count={len(plan.source.before_queue)} "
                f"transferred={transferred} terminal={len(plan.dispositions) - transferred} destinations={len(plan.destinations)}"
            )
        else:
            raise QueueTransferError("command and parsed arguments disagree")
    except (OSError, ReplaceError, QueueTransferError, TaskFrontmatterError, UnicodeDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
