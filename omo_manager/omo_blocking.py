#!/usr/bin/env python3
"""Versioned pending-item dependencies and durable wake reconciliation."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from omo_manager.omo_task_metadata import TASK_FRONTMATTER_V2
from omo_manager.omo_task_metadata import load_v2_mapping
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_lock import task_file_lock

V2_VERSION = TASK_FRONTMATTER_V2
ID_RE = re.compile(r"^(task|pi|wake)_[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
RETRY_MINUTES = (1, 2, 4, 8, 15)
WAKE_SOURCE_PREFIX = "(from bidirectional blocking wake "
ENABLE_FILE = ".omo-task-v2-enabled.yaml"


class BlockingError(ValueError):
    pass


@dataclass(frozen=True)
class TaskDocument:
    root: Path | None
    path: Path
    metadata: dict[str, Any]
    body: str
    original: str
    stat: os.stat_result


@dataclass(frozen=True)
class TaskIndex:
    by_task_id: dict[str, TaskDocument]
    item_owner: dict[tuple[str, str], tuple[TaskDocument, str]]


@dataclass(frozen=True)
class ReconcileResult:
    changed_paths: tuple[Path, ...]
    errors: tuple[str, ...]


def generated_id(prefix: str) -> str:
    """Return a lowercase UUIDv7 identifier on Python versions without `uuid.uuid7`."""
    if prefix not in {"task", "pi", "wake"}:
        raise BlockingError("invalid generated id prefix")
    unix_ms = time.time_ns() // 1_000_000
    random_bits = int.from_bytes(os.urandom(10), "big")
    value = (unix_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= (random_bits & ((1 << 12) - 1)) << 64
    value |= 0b10 << 62
    value |= (random_bits >> 12) & ((1 << 62) - 1)
    return f"{prefix}_{uuid.UUID(int=value)}"


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_time(value: object, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif not isinstance(value, str):
        raise BlockingError(f"`{field}` must be an RFC 3339 timestamp or null")
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BlockingError(f"`{field}` must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise BlockingError(f"`{field}` must include an explicit offset")
    return parsed


def split_task_text(text: str) -> tuple[str, str]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise BlockingError("task file has no frontmatter")
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[1:idx]), "".join(lines[idx + 1 :])
    raise BlockingError("task frontmatter opening marker has no closing marker")


def load_yaml_mapping(frontmatter: str) -> dict[str, Any]:
    try:
        loaded = load_v2_mapping(frontmatter)
    except ValueError as exc:
        raise BlockingError(f"invalid task YAML: {exc}") from exc
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise BlockingError("task frontmatter must be a YAML mapping")
    return loaded


def load_task(path: Path, *, root: Path | None = None, require_v2: bool = True) -> TaskDocument:
    stat = path.stat()
    original = path.read_text(encoding="utf-8")
    frontmatter, body = split_task_text(original)
    metadata = load_yaml_mapping(frontmatter)
    try:
        parsed = parse_task_metadata(original, root)
    except ValueError as exc:
        raise BlockingError(f"invalid task metadata: {exc}") from exc
    if parsed is None:
        raise BlockingError("task file has no frontmatter")
    if require_v2 and metadata.get("version") != V2_VERSION:
        raise BlockingError(f"task metadata is not `{V2_VERSION}`")
    return TaskDocument(root.resolve() if root is not None else None, path.resolve(), metadata, body, original, stat)


def require_id(value: object, prefix: str, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(f"{prefix}_") or ID_RE.fullmatch(value) is None:
        raise BlockingError(f"`{field}` must be a canonical `{prefix}_` UUIDv7")
    return value


def render_task(metadata: dict[str, Any], body: str, work_log_root: Path | None = None) -> str:
    frontmatter = yaml.safe_dump(metadata, allow_unicode=True, default_flow_style=False, sort_keys=False, width=160)
    rendered = f"---\n{frontmatter}---\n{body}"
    parsed = parse_task_metadata(rendered, work_log_root)
    if parsed is None or parsed.version != V2_VERSION:
        raise BlockingError("rendered task metadata is not valid v2")
    return rendered


def write_document(document: TaskDocument) -> None:
    updated = render_task(document.metadata, document.body, document.root)
    if updated == document.original:
        return
    with task_file_lock(document.path):
        current = document.path.stat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            document.stat.st_dev,
            document.stat.st_ino,
            document.stat.st_size,
            document.stat.st_mtime_ns,
        ):
            raise BlockingError("task changed concurrently; retry")
        mode = document.stat.st_mode & 0o777
        fd, tmp_name = tempfile.mkstemp(prefix=f".{document.path.name}.", dir=document.path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                _ = stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, document.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def document_with(document: TaskDocument, metadata: dict[str, Any], body: str | None = None) -> TaskDocument:
    return TaskDocument(document.root, document.path, metadata, document.body if body is None else body, document.original, document.stat)


def task_paths(root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    seen: set[Path] = set()
    from omo_manager.omo_agent_status import parse_task_lines, resolve_task_path

    live_sections = {"todo:current", "todo:human pending", "todo:low priority"}
    for task in parse_task_lines(root / "TODO.md"):
        if task.section not in live_sections or task.task_file == "TODO.md":
            continue
        path = resolve_task_path(root, task.task_file)
        if path is None:
            continue
        if path not in seen and path.is_file():
            paths.append(path)
            seen.add(path)
    return tuple(paths)


def v2_enabled(root: Path) -> bool:
    try:
        marker = yaml.safe_load((root / ENABLE_FILE).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False
    if marker != {"version": V2_VERSION, "enabled": True}:
        return False
    return True


def build_index(root: Path) -> TaskIndex:
    by_task_id: dict[str, TaskDocument] = {}
    item_owner: dict[tuple[str, str], tuple[TaskDocument, str]] = {}
    item_ids: set[str] = set()
    notice_ids: set[str] = set()
    for path in task_paths(root):
        try:
            document = load_task(path, root=root, require_v2=False)
        except (OSError, BlockingError) as exc:
            raise BlockingError("active task graph contains an unreadable or malformed task") from exc
        if document.metadata.get("version") != V2_VERSION:
            raise BlockingError("active task graph contains a non-v2 task after enablement")
        task_id = document.metadata["task_id"]
        if task_id in by_task_id:
            raise BlockingError("duplicate task id in active task graph")
        by_task_id[task_id] = document
        for state, field in (("pending", "pending_task_items"), ("resolved", "resolved_task_items")):
            for item in document.metadata[field]:
                key = (task_id, item["id"])
                if item["id"] in item_ids:
                    raise BlockingError("duplicate item id in active task graph")
                item_ids.add(item["id"])
                item_owner[key] = (document, state)
                for notice in item["notices"]:
                    if notice["id"] in notice_ids:
                        raise BlockingError("duplicate notice id in active task graph")
                    notice_ids.add(notice["id"])
    return TaskIndex(by_task_id, item_owner)


def pending_item(metadata: dict[str, Any], item_id: str) -> dict[str, Any]:
    matches = [item for item in metadata["pending_task_items"] if item["id"] == item_id]
    if len(matches) != 1:
        raise BlockingError("pending item id not found")
    return matches[0]


def external_blockers(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [blocker for blocker in metadata.get("blocked_on", []) if blocker.get("kind") not in {"pending_items", "persistent"}]


def persistent_blockers(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [blocker for blocker in metadata.get("blocked_on", []) if blocker.get("kind") == "persistent"]


def sync_generated_blocker(metadata: dict[str, Any]) -> bool:
    blocked_ids = [item["id"] for item in metadata["pending_task_items"] if item["blocked_on"]]
    external = external_blockers(metadata)
    persistent = persistent_blockers(metadata)
    old = metadata.get("blocked_on", [])
    new: list[dict[str, Any]] = []
    if blocked_ids:
        new.append({"kind": "pending_items", "item_ids": blocked_ids})
    new.extend(external)
    new.extend(persistent)
    if blocked_ids or external:
        metadata["blocked_on"] = new
        if metadata["status"] != "blocked":
            metadata["resume_status"] = metadata["status"]
            metadata["status"] = "blocked"
        elif metadata.get("resume_status") not in {"running", "long_running"}:
            raise BlockingError("blocked task is missing a valid resume status")
    elif persistent:
        metadata["blocked_on"] = persistent
        if metadata["status"] == "blocked":
            metadata["status"] = metadata.pop("resume_status")
            if metadata["status"] != "long_running":
                raise BlockingError("persistent blocker requires long_running resume status")
    elif metadata["status"] == "blocked" and old and all(blocker.get("kind") == "pending_items" for blocker in old):
        metadata["status"] = metadata.pop("resume_status")
        metadata.pop("blocked_on", None)
    return old != metadata.get("blocked_on", [])


def current_notice(item: dict[str, Any], kind: str = "ready") -> dict[str, Any] | None:
    for notice in reversed(item["notices"]):
        if notice["kind"] == kind and notice["state"] in {"pending", "deferred", "acked"}:
            return notice
    return None


def supersede_current_notices(item: dict[str, Any]) -> None:
    for notice in item["notices"]:
        if notice["state"] in {"pending", "deferred", "acked"}:
            notice["state"] = "superseded"


def new_notice(metadata: dict[str, Any], kind: str, state: str) -> dict[str, Any]:
    return {
        "id": generated_id("wake"),
        "kind": kind,
        "state": state,
        "recipient_task_id": metadata["task_id"],
        "target_snapshot": metadata["runat"],
        "attempt_count": 0,
        "retry_after": None,
        "escalated_at": None,
    }


def graph_edges(index: TaskIndex, extra: tuple[tuple[str, str], tuple[str, str]] | None = None) -> dict[tuple[str, str], set[tuple[str, str]]]:
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for task_id, document in index.by_task_id.items():
        for item in document.metadata["pending_task_items"]:
            node = (task_id, item["id"])
            edges[node] = {(dependency["task_id"], dependency["item_id"]) for dependency in item["blocked_on"]}
    if extra is not None:
        edges.setdefault(extra[0], set()).add(extra[1])
    return edges


def reject_cycle(edges: dict[tuple[str, str], set[tuple[str, str]]]) -> None:
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(node: tuple[str, str]) -> None:
        if node in visiting:
            raise BlockingError("dependency would create a cycle")
        if node in visited:
            return
        visiting.add(node)
        for target in edges.get(node, set()):
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in edges:
        visit(node)


def cyclic_nodes(edges: dict[tuple[str, str], set[tuple[str, str]]]) -> set[tuple[str, str]]:
    """Return every node belonging to a strongly connected dependency component."""
    next_index = 0
    indices: dict[tuple[str, str], int] = {}
    lowlinks: dict[tuple[str, str], int] = {}
    stack: list[tuple[str, str]] = []
    stacked: set[tuple[str, str]] = set()
    result: set[tuple[str, str]] = set()

    def connect(node: tuple[str, str]) -> None:
        nonlocal next_index
        indices[node] = next_index
        lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        stacked.add(node)
        for target in edges.get(node, set()):
            if target not in indices:
                connect(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in stacked:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: set[tuple[str, str]] = set()
        while stack:
            candidate = stack.pop()
            stacked.remove(candidate)
            component.add(candidate)
            if candidate == node:
                break
        if len(component) > 1 or node in edges.get(node, set()):
            result.update(component)

    for node in set(edges).union(*(set(targets) for targets in edges.values())):
        if node not in indices:
            connect(node)
    return result


def add_dependency(root: Path, owner_path: Path, item_id: str, source_path: Path, source_item_id: str) -> None:
    owner = load_task(owner_path, root=root)
    source = load_task(source_path, root=root)
    item = pending_item(owner.metadata, item_id)
    if owner.metadata["task_id"] == source.metadata["task_id"] and item_id == source_item_id:
        raise BlockingError("an item cannot depend on itself")
    source_matches = [candidate for candidate in source.metadata["pending_task_items"] if candidate["id"] == source_item_id]
    if not source_matches:
        resolved = [candidate for candidate in source.metadata["resolved_task_items"] if candidate["id"] == source_item_id]
        if resolved and resolved[0]["outcome"] == "cancelled":
            raise BlockingError("cannot depend on a cancelled item")
        raise BlockingError("referenced pending item id not found")
    dependency = {"task_id": source.metadata["task_id"], "item_id": source_item_id, "state": "waiting"}
    if any(candidate["task_id"] == dependency["task_id"] and candidate["item_id"] == source_item_id for candidate in item["blocked_on"]):
        raise BlockingError("dependency already exists")
    index = build_index(root)
    if owner.metadata["task_id"] not in index.by_task_id or source.metadata["task_id"] not in index.by_task_id:
        raise BlockingError("both dependency tasks must be active in the task registry")
    reject_cycle(graph_edges(index, ((owner.metadata["task_id"], item_id), (source.metadata["task_id"], source_item_id))))
    supersede_current_notices(item)
    item["blocked_on"].append(dependency)
    _ = sync_generated_blocker(owner.metadata)
    write_document(document_with(owner, owner.metadata, prune_stale_wake_markers(owner.metadata, owner.body)))


def remove_dependency(root: Path, owner_path: Path, item_id: str, source_task_id: str, source_item_id: str, evidence: str) -> None:
    owner = load_task(owner_path, root=root)
    item = pending_item(owner.metadata, item_id)
    matches = [dependency for dependency in item["blocked_on"] if dependency["task_id"] == source_task_id and dependency["item_id"] == source_item_id]
    if len(matches) != 1:
        raise BlockingError("dependency not found")
    item["blocked_on"].remove(matches[0])
    supersede_current_notices(item)
    if not item["blocked_on"]:
        state = "deferred" if external_blockers(owner.metadata) else "pending"
        item["notices"].append(new_notice(owner.metadata, "ready", state))
    _ = sync_generated_blocker(owner.metadata)
    owner_body = owner.body
    newline = "" if not owner_body or owner_body.endswith("\n") else "\n"
    owner_body = f"{owner_body}{newline}(verified dependency removal: {evidence})\n"
    owner_body = prune_stale_wake_markers(owner.metadata, owner_body)
    write_document(document_with(owner, owner.metadata, owner_body))


def add_items(document: TaskDocument, texts: tuple[str, ...]) -> tuple[str, ...]:
    if document.metadata["status"] == "done":
        raise BlockingError("task is already done")
    values = tuple(text.strip() for text in texts)
    if not values or any(not value or "\n" in value or "\r" in value for value in values):
        raise BlockingError("pending item must be nonempty one-line text")
    existing = {item["text"] for item in document.metadata["pending_task_items"]}
    if len(set(values)) != len(values) or any(value in existing for value in values):
        raise BlockingError("pending item text already exists")
    item_ids = tuple(generated_id("pi") for _value in values)
    document.metadata["pending_task_items"].extend(
        {"id": item_id, "text": value, "blocked_on": [], "notices": []} for item_id, value in zip(item_ids, values, strict=True)
    )
    write_document(document)
    return item_ids


def add_item(document: TaskDocument, text: str) -> str:
    return add_items(document, (text,))[0]


def replace_item(document: TaskDocument, item_id: str, text: str) -> None:
    item = pending_item(document.metadata, item_id)
    value = text.strip()
    if not value or "\n" in value or "\r" in value:
        raise BlockingError("replacement item must be nonempty one-line text")
    if any(candidate["id"] != item_id and candidate["text"] == value for candidate in document.metadata["pending_task_items"]):
        raise BlockingError("replacement pending item text already exists")
    item["text"] = value
    write_document(document)


def resolve_item(document: TaskDocument, item_id: str, outcome: str, evidence: str) -> None:
    if outcome not in {"completed", "cancelled"}:
        raise BlockingError("outcome must be `completed` or `cancelled`")
    value = evidence.strip()
    if not value or "\n" in value or "\r" in value:
        raise BlockingError("evidence must be nonempty one-line text")
    item = pending_item(document.metadata, item_id)
    supersede_current_notices(item)
    document.metadata["pending_task_items"].remove(item)
    document.metadata["resolved_task_items"].append(
        {"id": item_id, "outcome": outcome, "evidence": value, "resolved_at": now_rfc3339(), "notices": item["notices"]}
    )
    _ = sync_generated_blocker(document.metadata)
    write_document(document_with(document, document.metadata, prune_stale_wake_markers(document.metadata, document.body)))


def reconcile(root: Path) -> ReconcileResult:
    index = build_index(root)
    cycle_items = cyclic_nodes(graph_edges(index))
    changed: list[Path] = []
    errors: list[str] = []
    if cycle_items:
        for task_id, document in index.by_task_id.items():
            document_changed = False
            for item in document.metadata["pending_task_items"]:
                if (task_id, item["id"]) in cycle_items and not any(
                    notice["kind"] == "cycle_repair" and notice["state"] != "superseded" for notice in item["notices"]
                ):
                    item["notices"].append(new_notice(document.metadata, "cycle_repair", "pending"))
                    document_changed = True
                for notice in item["notices"]:
                    if notice["kind"] == "ready" and notice["state"] in {"pending", "acked", "deferred"}:
                        notice["state"] = "superseded"
                        document_changed = True
            if sync_generated_blocker(document.metadata):
                document_changed = True
            body = prune_stale_wake_markers(document.metadata, document.body)
            if body != document.body:
                document_changed = True
            if document_changed:
                write_document(document_with(document, document.metadata, body))
                changed.append(document.path)
        return ReconcileResult(tuple(changed), ("dependency cycle requires manager repair",))
    for document in index.by_task_id.values():
        metadata = document.metadata
        document_changed = False
        body = document.body
        for item in metadata["pending_task_items"]:
            for notice in item["notices"]:
                if notice["kind"] == "cycle_repair" and notice["state"] != "superseded":
                    notice["state"] = "superseded"
                    document_changed = True
            completed_reference = False
            for dependency in list(item["blocked_on"]):
                source = index.item_owner.get((dependency["task_id"], dependency["item_id"]))
                if source is None:
                    errors.append(f"missing dependency for item {item['id']}")
                    continue
                source_document, source_state = source
                if source_state == "pending":
                    if dependency["state"] != "waiting":
                        dependency["state"] = "waiting"
                        document_changed = True
                    continue
                resolved = next(candidate for candidate in source_document.metadata["resolved_task_items"] if candidate["id"] == dependency["item_id"])
                if resolved["outcome"] == "completed":
                    item["blocked_on"].remove(dependency)
                    completed_reference = True
                    document_changed = True
                elif dependency["state"] != "cancelled":
                    dependency["state"] = "cancelled"
                    supersede_current_notices(item)
                    item["notices"].append(new_notice(metadata, "dependency_cancelled", "pending"))
                    document_changed = True
            if item["blocked_on"]:
                notice = current_notice(item)
                if notice is not None and notice["kind"] == "ready":
                    notice["state"] = "superseded"
                    document_changed = True
            if completed_reference and not item["blocked_on"]:
                supersede_current_notices(item)
                item["notices"].append(new_notice(metadata, "ready", "deferred" if external_blockers(metadata) else "pending"))
        if sync_generated_blocker(metadata):
            document_changed = True
        external = external_blockers(metadata)
        for item in metadata["pending_task_items"]:
            ready = current_notice(item)
            if ready is None:
                continue
            if external and ready["state"] in {"pending", "acked"}:
                ready["state"] = "superseded"
                item["notices"].append(new_notice(metadata, "ready", "deferred"))
                document_changed = True
            elif not external and ready["state"] == "deferred":
                ready["state"] = "pending"
                ready["retry_after"] = None
                document_changed = True
        pruned_body = prune_stale_wake_markers(metadata, body)
        if pruned_body != body:
            body = pruned_body
            document_changed = True
        if document_changed:
            write_document(document_with(document, metadata, body))
            changed.append(document.path)
    return ReconcileResult(tuple(changed), tuple(errors))


def retry_after(attempt_count: int, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc).astimezone()
    minutes = RETRY_MINUTES[min(max(attempt_count, 1), len(RETRY_MINUTES)) - 1]
    return datetime.fromtimestamp(current.timestamp() + minutes * 60, tz=current.tzinfo).isoformat(timespec="seconds")


def notice_marker(notice_id: str) -> str:
    return f"{WAKE_SOURCE_PREFIX}{notice_id})"


def only_wake_pending_markers(text: str) -> bool:
    lines = text.splitlines()
    marker_indices = [idx for idx, line in enumerate(lines) if line.strip() == "(pending)"]
    return bool(marker_indices) and all(
        idx + 1 < len(lines) and "bidirectional blocking" in lines[idx + 1] for idx in marker_indices
    )


def remove_wake_marker(body: str, notice_id: str) -> str:
    lines = body.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if notice_id not in line or "bidirectional blocking" not in line:
            continue
        start = idx - 1 if idx > 0 and lines[idx - 1].strip() == "(pending)" else idx
        end = idx + 1
        while end < len(lines) and (
            lines[end].startswith("Pending item ready:")
            or lines[end].startswith("Run `omo_pending.py wake-ack")
            or lines[end].startswith("Blocking dependency was cancelled")
            or lines[end].startswith("Pending-item wake delivery needs manager attention")
        ):
            end += 1
        del lines[start:end]
        return remove_wake_marker("".join(lines), notice_id)
    return body


def prune_stale_wake_markers(metadata: dict[str, Any], body: str) -> str:
    updated = body
    for field in ("pending_task_items", "resolved_task_items"):
        for item in metadata[field]:
            for notice in item["notices"]:
                if notice["state"] != "pending":
                    updated = remove_wake_marker(updated, notice["id"])
    return updated


def marker_present(body: str, notice_id: str) -> bool:
    return f"wake {notice_id})" in body


def append_wake_marker(body: str, item: dict[str, Any], notice: dict[str, Any]) -> str:
    separator = "" if not body or body.endswith("\n") else "\n"
    if notice["kind"] == "ready":
        source = notice_marker(notice["id"])
        message = f"Pending item ready: {item['id']} {item['text']}\nRun `omo_pending.py wake-ack --notice-id {notice['id']}` after receiving this notice."
    elif notice["kind"] == "dependency_cancelled":
        source = f"(from manager bidirectional blocking wake {notice['id']})"
        message = f"Blocking dependency was cancelled for pending item {item['id']}: {item['text']}. Ask the manager to remove or replace the dependency."
    else:
        source = f"(from manager bidirectional blocking wake {notice['id']})"
        message = f"A manually introduced dependency cycle includes pending item {item['id']}: {item['text']}. Remove one incorrect edge; ready wakes remain suppressed until full revalidation."
    return f"{body}{separator}(pending)\n{source}\n{message}\n"


def append_escalation_marker(body: str, item: dict[str, Any], notice: dict[str, Any]) -> str:
    separator = "" if not body or body.endswith("\n") else "\n"
    return (
        f"{body}{separator}(pending)\n(from manager bidirectional blocking escalation {notice['id']})\n"
        f"Pending-item wake delivery needs manager attention after five attempts: {item['id']} {item['text']}.\n"
    )


def queue_due_notices(root: Path, now: datetime | None = None) -> tuple[Path, ...]:
    reconciled = reconcile(root)
    non_cycle_errors = [error for error in reconciled.errors if error != "dependency cycle requires manager repair"]
    if non_cycle_errors:
        raise BlockingError(non_cycle_errors[0])
    current = now or datetime.now(timezone.utc).astimezone()
    changed: list[Path] = []
    index = build_index(root)
    for document in index.by_task_id.values():
        metadata = document.metadata
        body = document.body
        queued = False
        for item in metadata["pending_task_items"]:
            for notice in item["notices"]:
                if notice["state"] != "pending" or marker_present(body, notice["id"]):
                    continue
                if notice["target_snapshot"] != metadata["runat"]:
                    notice["retry_after"] = None
                    notice["escalated_at"] = None
                due = parse_time(notice["retry_after"], "notice.retry_after")
                if due is not None and due > current:
                    continue
                notice["recipient_task_id"] = metadata["task_id"]
                notice["target_snapshot"] = metadata["runat"]
                notice["attempt_count"] += 1
                notice["retry_after"] = retry_after(notice["attempt_count"], current)
                if notice["attempt_count"] >= 5 and notice["escalated_at"] is None:
                    notice["escalated_at"] = current.isoformat(timespec="seconds")
                    body = append_escalation_marker(body, item, notice)
                body = append_wake_marker(body, item, notice)
                queued = True
        if queued:
            write_document(document_with(document, metadata, body))
            changed.append(document.path)
    return tuple(changed)


def acknowledge(document: TaskDocument, notice_id: str) -> tuple[str, str]:
    require_id(notice_id, "wake", "notice id")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in document.metadata["pending_task_items"]:
        for notice in item["notices"]:
            if notice["id"] == notice_id:
                matches.append((item, notice))
    if len(matches) != 1:
        raise BlockingError("wake notice id not found on the current task")
    item, notice = matches[0]
    if notice["recipient_task_id"] != document.metadata["task_id"]:
        raise BlockingError("wake notice recipient does not match the current task")
    if notice["kind"] != "ready" or item["blocked_on"] or external_blockers(document.metadata):
        if notice["state"] != "superseded":
            notice["state"] = "superseded"
            write_document(document)
        raise BlockingError("wake notice is stale; the item is still blocked")
    if notice["state"] == "acked":
        return item["id"], item["text"]
    if notice["state"] != "pending":
        raise BlockingError("wake notice is no longer current")
    notice["state"] = "acked"
    notice["escalated_at"] = None
    body = remove_wake_marker(document.body, notice_id)
    write_document(document_with(document, document.metadata, body))
    return item["id"], item["text"]


def task_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
