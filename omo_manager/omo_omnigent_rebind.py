#!/usr/bin/env python3
"""Rebind one task after an authenticated OmniGent native-thread rotation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import cast

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_omnigent import request_json, session_id, target_for_session
from omo_manager.omo_repository_custody import publish_or_validate
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import (
    authoritative_active_target_task_paths,
    current_target_task_paths,
    replace_if_unchanged_locked,
    root_membership_lock,
)

PACKET_SCHEMA = "omo-omnigent-rotation-rebind/v1"
REVIEW_SCHEMA = "omo-omnigent-rotation-rebind-review/v1"
AUDIT_SCHEMA = "omo-omnigent-rotation-rebind-audit/v1"
BRIDGE_LABEL = "omnigent.codex_native.bridge_id"
TERMINAL_ID = "terminal_codex_main"


class RebindError(RuntimeError):
    """A rotation-rebind invariant failed without an unauthenticated write."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    data: bytes
    state: os.stat_result


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def object_map(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RebindError(f"{label} is not an object")
    return cast(dict[str, object], value)


def required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RebindError(f"{label} is missing")
    return value


def decode(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise RebindError(f"{label} is not base64 text")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise RebindError(f"{label} is not canonical base64") from exc


def file_snapshot(path: Path, label: str) -> FileSnapshot:
    if not path.is_absolute() or path.is_symlink():
        raise RebindError(f"{label} must be one absolute non-symlink path")
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise RebindError(f"cannot read {label}: {exc}") from exc
    def identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
    if identity(before) != identity(after):
        raise RebindError(f"{label} changed while read")
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
        raise RebindError(f"{label} is not an owner-controlled regular file")
    return FileSnapshot(path, data, before)


def helper_binding() -> dict[str, object]:
    path = Path(__file__).resolve(strict=True)
    snapshot = file_snapshot(path, "rotation rebind helper")
    return {"path": str(path), "sha256": digest(snapshot.data)}


def path_in_root(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise RebindError("task file must be one relative path inside the work-log root")
    path = (root / relative).resolve(strict=False)
    if path == root or root not in path.parents:
        raise RebindError("task file escapes the work-log root")
    return path


def task_body(data: bytes) -> bytes:
    if not data.startswith(b"---\n"):
        raise RebindError("task does not begin with v1 frontmatter")
    marker = data.find(b"\n---\n", 4)
    if marker < 0:
        raise RebindError("task frontmatter is not closed")
    return data[marker + len(b"\n---\n") :]


def replace_runat(data: bytes, old_target: str, new_target: str) -> bytes:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise RebindError("task does not begin with frontmatter")
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.rstrip("\r\n") == "---"), None)
    if closing is None:
        raise RebindError("task frontmatter is not closed")
    matches = [index for index in range(1, closing) if lines[index].rstrip("\r\n") == f"runat: {old_target}"]
    if len(matches) != 1:
        raise RebindError("task frontmatter does not contain exactly the authenticated old runat")
    index = matches[0]
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"runat: {new_target}{newline}"
    return "".join(lines).encode()


def todo_rebind(data: bytes, task_name: str, old_target: str, new_target: str) -> bytes:
    text = data.decode("utf-8")
    lines = text.splitlines(keepends=True)
    section = ""
    old_rows: list[int] = []
    new_rows: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(":") and not stripped.startswith(('"', "'")):
            section = stripped[:-1]
            continue
        if stripped == f"{task_name} {old_target}":
            if section != "current":
                raise RebindError("authenticated task TODO row is not in current")
            old_rows.append(index)
        if stripped == f"{task_name} {new_target}":
            new_rows.append(index)
    if len(old_rows) != 1 or new_rows:
        raise RebindError("TODO does not contain exactly one old route and no replacement route")
    index = old_rows[0]
    newline = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"{task_name} {new_target}{newline}"
    return "".join(lines).encode()


def session_payload(target: str) -> dict[str, object]:
    identifier = session_id(target)
    value = request_json("GET", f"/v1/sessions/{identifier}?include_items=true&refresh_state=true")
    return object_map(value, f"OmniGent session {identifier}")


def normalized_item(value: object, label: str) -> dict[str, object]:
    item = object_map(value, label)
    data = object_map(item.get("data"), f"{label} data")
    result: dict[str, object] = {
        "id": required_text(item.get("id"), f"{label} id"),
        "type": required_text(item.get("type"), f"{label} type"),
        "status": required_text(item.get("status"), f"{label} status"),
        "response_id": required_text(item.get("response_id"), f"{label} response id"),
        "created_at": item.get("created_at"),
        "role": data.get("role"),
        "event_type": data.get("event_type"),
        "resource_id": data.get("resource_id"),
        "resource_type": data.get("resource_type"),
    }
    content = data.get("content")
    if content is not None:
        if not isinstance(content, list):
            raise RebindError(f"{label} content is malformed")
        texts: list[str] = []
        for block in content:
            mapping = object_map(block, f"{label} content block")
            if mapping.get("type") != "input_text" or not isinstance(mapping.get("text"), str):
                raise RebindError(f"{label} is not a plain input_text message")
            texts.append(str(mapping["text"]))
        prompt = "".join(texts).encode()
        result["content_sha256"] = digest(prompt)
        result["content_size_bytes"] = len(prompt)
    return result


def normalized_session(value: dict[str, object], label: str) -> dict[str, object]:
    labels = object_map(value.get("labels"), f"{label} labels")
    items = value.get("items")
    if not isinstance(items, list):
        raise RebindError(f"{label} item history is missing")
    normalized_items = [normalized_item(item, f"{label} item {index}") for index, item in enumerate(cast(list[object], items))]
    return {
        "id": required_text(value.get("id"), f"{label} id"),
        "agent_id": required_text(value.get("agent_id"), f"{label} agent id"),
        "harness": required_text(value.get("harness"), f"{label} harness"),
        "status": required_text(value.get("status"), f"{label} status"),
        "runner_id": value.get("runner_id"),
        "runner_online": value.get("runner_online"),
        "host_id": value.get("host_id"),
        "host_online": value.get("host_online"),
        "workspace": value.get("workspace"),
        "title": value.get("title"),
        "external_session_id": value.get("external_session_id"),
        "created_at": value.get("created_at"),
        "updated_at": value.get("updated_at"),
        "bridge_id": labels.get(BRIDGE_LABEL),
        "items": normalized_items,
    }


def rotation_evidence(
    old_target: str,
    new_target: str,
    expected_workspace: str,
    expected_title: str,
    expected_prompt_sha256: str,
    expected_task_body: bytes,
) -> dict[str, object]:
    old_raw = session_payload(old_target)
    new_raw = session_payload(new_target)
    old = normalized_session(old_raw, "old session")
    new = normalized_session(new_raw, "rotated session")
    old_id = session_id(old_target)
    new_id = session_id(new_target)
    if old["id"] != old_id or new["id"] != new_id or old_id == new_id:
        raise RebindError("OmniGent session identities do not match the requested distinct targets")
    if old["agent_id"] != new["agent_id"] or old["harness"] != "codex-native" or new["harness"] != "codex-native":
        raise RebindError("old and rotated sessions do not bind the same codex-native agent")
    if old["workspace"] != expected_workspace or old["title"] != expected_title:
        raise RebindError("old session workspace or title differs from the authenticated task launch")
    if new["bridge_id"] != old_id:
        raise RebindError("rotated session does not carry the old session bridge lineage")
    old_runner_id = old["runner_id"]
    if (old_runner_id is not None and old_runner_id != "") or not isinstance(new["runner_id"], str) or not new["runner_id"]:
        raise RebindError("runner custody was not transferred from old session to rotated session")
    if new["runner_online"] is not True or new["status"] != "idle" or old["status"] != "running":
        raise RebindError("old/rotated session liveness does not match the recoverable rotation state")
    old_external_id = old["external_session_id"]
    new_external_id = new["external_session_id"]
    if (
        not isinstance(old_external_id, str)
        or not old_external_id
        or not isinstance(new_external_id, str)
        or not new_external_id
        or old_external_id == new_external_id
    ):
        raise RebindError("Codex native thread identities do not prove a thread rotation")
    old_items = cast(list[dict[str, object]], old["items"])
    new_items = cast(list[dict[str, object]], new["items"])
    old_shape = [(item["type"], item["status"], item["event_type"], item["resource_id"], item["resource_type"], item["role"]) for item in old_items]
    new_shape = [(item["type"], item["status"], item["event_type"], item["resource_id"], item["resource_type"], item["role"]) for item in new_items]
    if old_shape != [
        ("resource_event", "completed", "session.resource.created", TERMINAL_ID, "terminal", None),
        ("message", "completed", None, None, None, "user"),
        ("resource_event", "completed", "session.resource.deleted", TERMINAL_ID, "terminal", None),
    ]:
        raise RebindError("old session history is not exactly completed terminal-create, completed user prompt, terminal-transfer")
    if new_shape != [("resource_event", "completed", "session.resource.created", TERMINAL_ID, "terminal", None)]:
        raise RebindError("rotated session is not an empty terminal-transfer target")
    prompt_item = object_map(old_items[1], "old prompt evidence")
    if prompt_item.get("content_sha256") != expected_prompt_sha256:
        raise RebindError("old session prompt differs from the authenticated prompt digest")
    old_raw_items = old_raw.get("items")
    if not isinstance(old_raw_items, list):
        raise RebindError("old session item history disappeared")
    old_content = object_map(cast(list[object], old_raw_items)[1], "old prompt item").get("data")
    content = object_map(old_content, "old prompt data").get("content")
    assert isinstance(content, list)
    prompt_text = "".join(required_text(object_map(block, "old prompt block").get("text"), "old prompt text") for block in content).encode()
    if not prompt_text.endswith(expected_task_body):
        raise RebindError("old session prompt does not end with the exact current task instructions")
    listing = object_map(request_json("GET", "/v1/sessions?limit=1000"), "OmniGent session list")
    listing_data = listing.get("data")
    if listing.get("has_more") is not False or not isinstance(listing_data, list):
        raise RebindError("OmniGent session inventory is incomplete")
    descendants: list[object] = []
    for row in cast(list[object], listing_data):
        mapping = object_map(row, "OmniGent session list row")
        labels = object_map(mapping.get("labels"), "OmniGent session list labels")
        if labels.get(BRIDGE_LABEL) == old_id:
            descendants.append(mapping.get("id"))
    if descendants != [new_id]:
        raise RebindError("old session has zero or multiple rotated descendants")
    return {"old": old, "new": new}


def task_and_todo_binding(
    root: Path,
    task_name: str,
    old_target: str,
    new_target: str,
    expected_task_sha256: str,
    expected_todo_sha256: str,
    expected_manager_target: str,
) -> tuple[FileSnapshot, FileSnapshot, bytes, bytes]:
    task_path = path_in_root(root, task_name)
    task = file_snapshot(task_path, "task")
    todo = file_snapshot(root / "TODO.md", "TODO")
    if digest(task.data) != expected_task_sha256 or digest(todo.data) != expected_todo_sha256:
        raise RebindError("task or TODO digest differs from the authenticated lifecycle state")
    try:
        metadata = parse_task_metadata(task.data.decode("utf-8"), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise RebindError(f"task metadata is invalid: {exc}") from exc
    if (
        metadata is None
        or metadata.version != "v1.0.0"
        or metadata.status != "blocked"
        or metadata.runat != old_target
        or metadata.tool != "codex"
        or metadata.managerat != expected_manager_target
        or metadata.is_manager
        or metadata.pending_task_items
        or not metadata.blocked_on
    ):
        raise RebindError("task is not the exact blocked queue-empty codex worker owned by the expected manager")
    task_path_resolved = task_path.resolve()
    if authoritative_active_target_task_paths(root, old_target) != (task_path_resolved,):
        raise RebindError("old target is not singularly owned by the selected task")
    if current_target_task_paths(root, old_target) != (task_path_resolved,):
        raise RebindError("old target TODO ownership is not singular and current")
    if authoritative_active_target_task_paths(root, new_target) or current_target_task_paths(root, new_target):
        raise RebindError("rotated target already has task ownership")
    updated_task = replace_runat(task.data, old_target, new_target)
    updated_todo = todo_rebind(todo.data, task_name, old_target, new_target)
    try:
        updated_metadata = parse_task_metadata(updated_task.decode("utf-8"), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise RebindError(f"updated task metadata would be invalid: {exc}") from exc
    if updated_metadata is None or updated_metadata.runat != new_target:
        raise RebindError("updated task does not bind the rotated target")
    return task, todo, updated_task, updated_todo


def build_packet(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve(strict=True)
    task, todo, updated_task, updated_todo = task_and_todo_binding(
        root,
        args.task_file,
        args.old_target,
        args.new_target,
        args.expected_task_sha256,
        args.expected_todo_sha256,
        args.expected_manager_target,
    )
    evidence = rotation_evidence(
        args.old_target,
        args.new_target,
        args.expected_workspace,
        args.expected_title,
        args.expected_prompt_sha256,
        task_body(task.data),
    )
    return {
        "schema": PACKET_SCHEMA,
        "operation": "rebind-authenticated-native-thread-rotation",
        "helper": helper_binding(),
        "root": str(root),
        "task": args.task_file,
        "old_target": args.old_target,
        "new_target": args.new_target,
        "manager_target": args.expected_manager_target,
        "workspace": args.expected_workspace,
        "title": args.expected_title,
        "prompt_sha256": args.expected_prompt_sha256,
        "task_before": base64.b64encode(task.data).decode(),
        "task_before_sha256": digest(task.data),
        "task_after": base64.b64encode(updated_task).decode(),
        "task_after_sha256": digest(updated_task),
        "todo_before": base64.b64encode(todo.data).decode(),
        "todo_before_sha256": digest(todo.data),
        "todo_after": base64.b64encode(updated_todo).decode(),
        "todo_after_sha256": digest(updated_todo),
        "rotation": evidence,
        "audit_output": str(args.audit_output.resolve(strict=False)),
    }


def packet_args(packet: dict[str, object]) -> argparse.Namespace:
    return argparse.Namespace(
        root=Path(required_text(packet.get("root"), "packet root")),
        task_file=required_text(packet.get("task"), "packet task"),
        old_target=required_text(packet.get("old_target"), "packet old target"),
        new_target=required_text(packet.get("new_target"), "packet new target"),
        expected_task_sha256=required_text(packet.get("task_before_sha256"), "packet task digest"),
        expected_todo_sha256=required_text(packet.get("todo_before_sha256"), "packet TODO digest"),
        expected_manager_target=required_text(packet.get("manager_target"), "packet manager target"),
        expected_workspace=required_text(packet.get("workspace"), "packet workspace"),
        expected_title=required_text(packet.get("title"), "packet title"),
        expected_prompt_sha256=required_text(packet.get("prompt_sha256"), "packet prompt digest"),
        audit_output=Path(required_text(packet.get("audit_output"), "packet audit output")),
    )


def load_record(path: Path, expected_sha256: str, label: str) -> tuple[dict[str, object], bytes]:
    snapshot = file_snapshot(path.resolve(strict=True), label)
    if stat.S_IMODE(snapshot.state.st_mode) != 0o600:
        raise RebindError(f"{label} is not owner-private")
    if digest(snapshot.data) != expected_sha256:
        raise RebindError(f"{label} SHA-256 differs")
    try:
        record = object_map(json.loads(snapshot.data), label)
    except json.JSONDecodeError as exc:
        raise RebindError(f"{label} is not valid JSON") from exc
    return record, snapshot.data


def validate_packet(packet: dict[str, object]) -> None:
    if packet.get("schema") != PACKET_SCHEMA or packet.get("operation") != "rebind-authenticated-native-thread-rotation":
        raise RebindError("rotation rebind packet schema or operation is invalid")
    helper = object_map(packet.get("helper"), "packet helper")
    if helper != helper_binding():
        raise RebindError("rotation rebind helper changed after preparation")
    for before_key, after_key, before_digest, after_digest in (
        ("task_before", "task_after", "task_before_sha256", "task_after_sha256"),
        ("todo_before", "todo_after", "todo_before_sha256", "todo_after_sha256"),
    ):
        before = decode(packet.get(before_key), before_key)
        after = decode(packet.get(after_key), after_key)
        if digest(before) != packet.get(before_digest) or digest(after) != packet.get(after_digest) or before == after:
            raise RebindError("rotation rebind packet file bytes or digests are invalid")


def prepare(args: argparse.Namespace) -> None:
    if not args.output.is_absolute() or not args.audit_output.is_absolute():
        raise RebindError("packet and audit outputs must be absolute paths")
    root = args.root.resolve(strict=True)
    task_path = path_in_root(root, args.task_file)
    with root_membership_lock(root), task_target_lock(root, args.old_target), task_target_lock(root, args.new_target), ExitStack() as locks:
        locks.enter_context(task_file_lock(task_path))
        locks.enter_context(task_file_lock(root / "TODO.md"))
        packet = build_packet(args)
    publish_or_validate(args.output, canonical(packet), "OmniGent rotation rebind packet")
    print(digest(canonical(packet)))


def review(args: argparse.Namespace) -> None:
    packet, packet_data = load_record(args.packet, args.packet_sha256, "rotation rebind packet")
    validate_packet(packet)
    root = Path(required_text(packet.get("root"), "packet root"))
    task_path = path_in_root(root, required_text(packet.get("task"), "packet task"))
    packet_parameters = packet_args(packet)
    with root_membership_lock(root), task_target_lock(root, packet_parameters.old_target), task_target_lock(root, packet_parameters.new_target), ExitStack() as locks:
        locks.enter_context(task_file_lock(task_path))
        locks.enter_context(task_file_lock(root / "TODO.md"))
        if canonical(build_packet(packet_parameters)) != packet_data:
            raise RebindError("current task, TODO, session, or rotation lineage differs from the packet")
    report = {
        "schema": REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": args.packet_sha256,
        "helper_sha256": object_map(packet["helper"], "packet helper")["sha256"],
    }
    publish_or_validate(args.review_output, canonical(report), "OmniGent rotation rebind review")


def current_file_state(path: Path, before: bytes, after: bytes, label: str) -> tuple[str, FileSnapshot]:
    snapshot = file_snapshot(path, label)
    if snapshot.data == before:
        return "before", snapshot
    if snapshot.data == after:
        return "after", snapshot
    raise RebindError(f"{label} differs from both reviewed transaction states")


def validate_final(packet: dict[str, object], task_path: Path, todo_path: Path) -> None:
    root = Path(required_text(packet.get("root"), "packet root"))
    new_target = required_text(packet.get("new_target"), "packet new target")
    old_target = required_text(packet.get("old_target"), "packet old target")
    task = file_snapshot(task_path, "rebound task")
    todo = file_snapshot(todo_path, "rebound TODO")
    if digest(task.data) != packet.get("task_after_sha256") or digest(todo.data) != packet.get("todo_after_sha256"):
        raise RebindError("rebound task or TODO bytes differ from the reviewed result")
    metadata = parse_task_metadata(task.data.decode("utf-8"), root)
    if metadata is None or metadata.runat != new_target or metadata.status != "blocked":
        raise RebindError("rebound task metadata does not preserve blocked custody on the rotated target")
    expected_owner = (task_path.resolve(),)
    if authoritative_active_target_task_paths(root, old_target) or current_target_task_paths(root, old_target):
        raise RebindError("old OmniGent target still has lifecycle ownership")
    if authoritative_active_target_task_paths(root, new_target) != expected_owner or current_target_task_paths(root, new_target) != expected_owner:
        raise RebindError("rotated target does not have singular task and TODO ownership")


def validate_intermediate_ownership(packet: dict[str, object], task_path: Path, task_state: str, todo_state: str) -> None:
    """Reject impossible or broadened task/TODO ownership before recovery writes."""

    root = Path(required_text(packet.get("root"), "packet root"))
    old_target = required_text(packet.get("old_target"), "packet old target")
    new_target = required_text(packet.get("new_target"), "packet new target")
    expected_owner = (task_path.resolve(),)
    state = (task_state, todo_state)
    if state not in {("before", "before"), ("before", "after"), ("after", "after")}:
        raise RebindError("task/TODO state is not a possible ordered rotation-rebind state")
    old_active = authoritative_active_target_task_paths(root, old_target)
    new_active = authoritative_active_target_task_paths(root, new_target)
    expected = {
        ("before", "before"): (expected_owner, ()),
        ("before", "after"): (expected_owner, ()),
        ("after", "after"): ((), expected_owner),
    }[state]
    if (old_active, new_active) != expected:
        raise RebindError("old/new target ownership drifted or gained a competing owner")


def execute(args: argparse.Namespace) -> None:
    packet, _packet_data = load_record(args.packet, args.packet_sha256, "rotation rebind packet")
    validate_packet(packet)
    review_record, _ = load_record(args.review_report, args.review_report_sha256, "rotation rebind review")
    expected_review = {
        "schema": REVIEW_SCHEMA,
        "verdict": "PASS",
        "packet_sha256": args.packet_sha256,
        "helper_sha256": object_map(packet["helper"], "packet helper")["sha256"],
    }
    if review_record != expected_review:
        raise RebindError("independent review does not PASS this exact packet and helper")
    root = Path(required_text(packet.get("root"), "packet root"))
    task_path = path_in_root(root, required_text(packet.get("task"), "packet task"))
    todo_path = root / "TODO.md"
    before_task = decode(packet.get("task_before"), "packet task before")
    after_task = decode(packet.get("task_after"), "packet task after")
    before_todo = decode(packet.get("todo_before"), "packet TODO before")
    after_todo = decode(packet.get("todo_after"), "packet TODO after")
    audit_output = Path(required_text(packet.get("audit_output"), "packet audit output"))
    if not audit_output.is_absolute():
        raise RebindError("audit output is not absolute")
    prepared = {
        "schema": AUDIT_SCHEMA,
        "state": "prepared",
        "packet_sha256": args.packet_sha256,
        "review_sha256": args.review_report_sha256,
        "task": str(task_path),
        "old_target": packet["old_target"],
        "new_target": packet["new_target"],
    }
    complete = dict(prepared)
    complete["state"] = "complete"
    complete["prepared_sha256"] = digest(canonical(prepared))
    complete["task_after_sha256"] = packet["task_after_sha256"]
    complete["todo_after_sha256"] = packet["todo_after_sha256"]
    prepared_bytes = canonical(prepared)
    complete_bytes = canonical(complete)
    old_target = required_text(packet.get("old_target"), "packet old target")
    new_target = required_text(packet.get("new_target"), "packet new target")
    with task_file_lock(audit_output):
        if audit_output.exists() or audit_output.is_symlink():
            audit = file_snapshot(audit_output, "rotation rebind audit")
            if stat.S_IMODE(audit.state.st_mode) != 0o600:
                raise RebindError("rotation rebind audit is not owner-private")
            if audit.data == complete_bytes:
                with root_membership_lock(root), task_target_lock(root, old_target), task_target_lock(root, new_target), ExitStack() as locks:
                    locks.enter_context(task_file_lock(task_path))
                    locks.enter_context(task_file_lock(todo_path))
                    validate_final(packet, task_path, todo_path)
                print(f"rebound {packet['task']} from {old_target} to {new_target}; audit={audit_output}")
                return
            if audit.data != prepared_bytes:
                raise RebindError("rotation rebind audit output conflicts with this transaction")
        else:
            publish_or_validate(audit_output, prepared_bytes, "prepared OmniGent rotation rebind audit")
            audit = file_snapshot(audit_output, "prepared rotation rebind audit")
        with root_membership_lock(root), task_target_lock(root, old_target), task_target_lock(root, new_target), ExitStack() as locks:
            locks.enter_context(task_file_lock(task_path))
            locks.enter_context(task_file_lock(todo_path))
            task_state, task = current_file_state(task_path, before_task, after_task, "task")
            todo_state, todo = current_file_state(todo_path, before_todo, after_todo, "TODO")
            validate_intermediate_ownership(packet, task_path, task_state, todo_state)
            if (task_state, todo_state) == ("before", "before"):
                packet_parameters = packet_args(packet)
                if canonical(build_packet(packet_parameters)) != canonical(packet):
                    raise RebindError("task, TODO, session, or rotation lineage drifted after review")
            else:
                _ = rotation_evidence(
                    old_target,
                    new_target,
                    required_text(packet.get("workspace"), "packet workspace"),
                    required_text(packet.get("title"), "packet title"),
                    required_text(packet.get("prompt_sha256"), "packet prompt digest"),
                    task_body(before_task),
                )
            published_todo: os.stat_result | None = None
            published_task: os.stat_result | None = None
            try:
                if todo_state == "before":
                    published_todo = replace_if_unchanged_locked(todo_path, after_todo.decode("utf-8"), todo.state)
                if task_state == "before":
                    published_task = replace_if_unchanged_locked(task_path, after_task.decode("utf-8"), task.state)
                _ = rotation_evidence(
                    old_target,
                    new_target,
                    required_text(packet.get("workspace"), "packet workspace"),
                    required_text(packet.get("title"), "packet title"),
                    required_text(packet.get("prompt_sha256"), "packet prompt digest"),
                    task_body(before_task),
                )
                validate_final(packet, task_path, todo_path)
                _ = replace_if_unchanged_locked(audit_output, complete_bytes.decode("utf-8"), audit.state)
            except Exception as exc:
                rollback_errors: list[str] = []
                if published_task is not None:
                    try:
                        _ = replace_if_unchanged_locked(task_path, before_task.decode("utf-8"), published_task)
                    except Exception as rollback_exc:  # noqa: BLE001 - preserve the primary transaction failure.
                        rollback_errors.append(f"task rollback failed: {rollback_exc}")
                if published_todo is not None:
                    try:
                        _ = replace_if_unchanged_locked(todo_path, before_todo.decode("utf-8"), published_todo)
                    except Exception as rollback_exc:  # noqa: BLE001 - preserve the primary transaction failure.
                        rollback_errors.append(f"TODO rollback failed: {rollback_exc}")
                if rollback_errors:
                    raise RebindError(f"rotation rebind failed and {'; '.join(rollback_errors)}") from exc
                raise
    print(f"rebound {packet['task']} from {old_target} to {new_target}; audit={audit_output}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--review", action="store_true")
    modes.add_argument("--execute", action="store_true")
    result.add_argument("--root", type=Path)
    result.add_argument("--task-file", default="")
    result.add_argument("--old-target", default="")
    result.add_argument("--new-target", default="")
    result.add_argument("--expected-task-sha256", default="")
    result.add_argument("--expected-todo-sha256", default="")
    result.add_argument("--expected-manager-target", default="")
    result.add_argument("--expected-workspace", default="")
    result.add_argument("--expected-title", default="")
    result.add_argument("--expected-prompt-sha256", default="")
    result.add_argument("--output", type=Path)
    result.add_argument("--audit-output", type=Path)
    result.add_argument("--packet", type=Path)
    result.add_argument("--packet-sha256", default="")
    result.add_argument("--review-output", type=Path)
    result.add_argument("--review-report", type=Path)
    result.add_argument("--review-report-sha256", default="")
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.prepare:
            required = (
                args.root,
                args.task_file,
                args.old_target,
                args.new_target,
                args.expected_task_sha256,
                args.expected_todo_sha256,
                args.expected_manager_target,
                args.expected_workspace,
                args.expected_title,
                args.expected_prompt_sha256,
                args.output,
                args.audit_output,
            )
            if not all(required):
                raise RebindError("prepare requires exact task, TODO, manager, workspace, title, prompt, target, packet, and audit bindings")
            _ = target_for_session(session_id(args.old_target))
            _ = target_for_session(session_id(args.new_target))
            prepare(args)
        elif args.review:
            if args.packet is None or not args.packet_sha256 or args.review_output is None:
                raise RebindError("review requires packet, packet SHA-256, and review output")
            review(args)
        else:
            if args.packet is None or not args.packet_sha256 or args.review_report is None or not args.review_report_sha256:
                raise RebindError("execute requires packet, packet SHA-256, review report, and review SHA-256")
            execute(args)
    except (OSError, RuntimeError, TaskFrontmatterError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"omo_omnigent_rebind.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
