#!/usr/bin/env python3
"""Prepare and apply one export-bound metadata-only agent closure."""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

import yaml

from omo_manager.omo_agent_status import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_metadata import UniqueKeyLoader, frontmatter_text
from omo_manager.omo_task_lock import task_file_lock, task_target_lock
from omo_manager.omo_task_status import (
    Args as StatusArgs,
    authoritative_active_target_task_paths,
    cleared_pending_task_text,
    has_export_then_close_authority,
    has_pending_marker,
    park_target_pane_id,
    relative_task_ref,
    read_park_authority,
    read_park_authority_envelope,
    root_membership_lock,
    todo_row_task_paths,
    update_frontmatter_status,
)
from omo_manager.omo_repository_custody import (
    CustodyError,
    HeldAbsolute,
    absolute_file_binding,
    authenticated_report,
    directory_identity_from,
    existing_exact,
    file_identity_from,
    hold_absolute,
    publish_or_validate,
    validate_held_absolute,
)
from omo_manager.omo_namespace_drain import stop_target, target_identity, inspect_target

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "omo-exported-agent-close/v1"
REVIEW_SCHEMA = "omo-exported-agent-close-review/v1"
# 🧑 "get all their pending task items, write them down into another file ... Then you simply close that agent"
MODES = {
    "absent-manager-previous",
    "absent-worker-unindexed",
    "shared-absent-manager",
    "shared-live-worker",
    "live-manager-terminal-children",
}
PACKET_KEYS = {
    "schema", "mode", "root", "task", "target", "pane_id", "task_before_sha256",
    "todo_before_sha256", "task_after_sha256", "todo_after_sha256", "task_after_base64",
    "todo_after_base64", "export", "export_sha256", "authority", "authority_sha256",
    "protected", "audit", "authority_envelope",
    "destination_target", "inputs", "authority_envelope_sha256", "binding_id",
    "pane_pid", "pane_start_ticks", "session_id", "children",
}


class IndeterminateClose(TaskFrontmatterError):
    """A displaced exact leaf is preserved, but automated recovery is unsafe."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular_unbound(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise TaskFrontmatterError(f"cannot open bound regular file {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        data = bytearray()
        while chunk := os.read(fd, 64 * 1024):
            data.extend(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise TaskFrontmatterError(f"bound regular file changed or is not regular: {path}")
    return bytes(data), before


def read_regular(path: Path, expected_sha256: str) -> tuple[bytes, os.stat_result]:
    data, state = read_regular_unbound(path)
    if sha256(data) != expected_sha256:
        raise TaskFrontmatterError(f"bound regular file has the wrong digest: {path}")
    return data, state


def export_section(export: str, task_ref: str) -> str:
    pattern = re.compile(rf"^## {re.escape(task_ref)}\s*$\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    matches = pattern.findall(export)
    if len(matches) != 1:
        raise TaskFrontmatterError("closure export must contain exactly one section for the task.")
    return matches[0]


def quoted_field(section: str, name: str) -> str:
    match = re.search(rf'^- {re.escape(name)}: ("(?:[^"\\]|\\.)*")\s*$', section, re.MULTILINE)
    if match is None:
        raise TaskFrontmatterError(f"closure export is missing canonical {name!r}.")
    value = json.loads(match.group(1))
    if not isinstance(value, str):
        raise TaskFrontmatterError(f"closure export {name!r} is not text.")
    return value


def plain_field(section: str, name: str) -> str:
    match = re.search(rf"^- {re.escape(name)}: ([^\r\n]+)$", section, re.MULTILINE)
    if match is None:
        raise TaskFrontmatterError(f"closure export is missing canonical {name!r}.")
    return match.group(1).strip().strip("`")


def validate_export(export: str, task_ref: str, task_sha256: str, metadata: object) -> None:
    section = export_section(export, task_ref)
    queue = list(metadata.pending_task_items)
    expected = {
        "target": metadata.runat,
        "manager": metadata.managerat,
        "status": metadata.status,
        "manager role": str(metadata.is_manager).lower(),
        "blocker": metadata.blocked_on,
    }
    if any(quoted_field(section, name) != value for name, value in expected.items()):
        raise TaskFrontmatterError("closure export provenance does not match the task.")
    queue_sha = sha256(json.dumps(queue, ensure_ascii=False, separators=(",", ":")).encode())
    if (
        plain_field(section, "task-file SHA-256") != task_sha256
        or plain_field(section, "ordered pending-item count") != str(len(queue))
        or plain_field(section, "ordered pending-items SHA-256") != queue_sha
    ):
        raise TaskFrontmatterError("closure export task or ordered-queue identity does not match.")
    if queue:
        found = re.findall(r'^  ([1-9][0-9]*)\. ("(?:[^"\\]|\\.)*")\s*$', section, re.MULTILINE)
        if [int(index) for index, _ in found] != list(range(1, len(queue) + 1)):
            raise TaskFrontmatterError("closure export item numbering is not contiguous.")
        if [json.loads(value) for _, value in found] != queue:
            raise TaskFrontmatterError("closure export does not preserve the complete ordered queue.")
    elif "- ordered pending items: none" not in section:
        raise TaskFrontmatterError("closure export does not canonically preserve the empty queue.")


def todo_rows(root: Path, task: Path, text: str) -> tuple[list[str], list[tuple[int, str]]]:
    lines = text.splitlines(keepends=True)
    section = ""
    headers: dict[str, int] = {name: 0 for name in ("current", "human pending", "low priority", "previous")}
    rows: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.endswith(":") and stripped[:-1].casefold() in headers:
            section = stripped[:-1].casefold()
            headers[section] += 1
        elif task in todo_row_task_paths(root, line):
            rows.append((index, section))
    if any(count != 1 for count in headers.values()):
        raise TaskFrontmatterError("TODO must contain each canonical lifecycle section exactly once.")
    return lines, rows


def closed_todo(root: Path, task: Path, text: str, mode: str, target: str) -> str:
    lines, rows = todo_rows(root, task, text)
    ref = relative_task_ref(root, task)
    if mode == "absent-worker-unindexed":
        if rows:
            raise TaskFrontmatterError("unindexed closure requires no TODO row.")
    else:
        allowed_sections = (
            {"previous"}
            if mode == "absent-manager-previous"
            else {"current", "human pending"}
            if mode == "shared-live-worker"
            else {"current"}
        )
        if len(rows) != 1 or rows[0][1] not in allowed_sections:
            raise TaskFrontmatterError("closure TODO row is absent, duplicated, or in the wrong section.")
        index, _ = rows[0]
        if lines[index].strip() != f"{ref} {target}":
            raise TaskFrontmatterError("closure requires one canonical targetful TODO row.")
        lines.pop(index)
    previous = next(index for index, line in enumerate(lines) if line.rstrip("\r\n") == "previous:")
    lines.insert(previous + 1, ref + "\n")
    return "".join(lines)


def closed_task(root: Path, task: Path, text: str, export: Path, export_sha256: str, mode: str) -> str:
    cleared = cleared_pending_task_text(text, root)
    updated = update_frontmatter_status(cleared, "done", "", root).rstrip("\n")
    pane_result = "exact bound pane closed" if mode == "live-manager-terminal-children" else "tmux unchanged"
    note = (
        f"(Source-1398 exported-agent closure: ordered queue and provenance preserved in {export}; "
        f"export SHA-256: {export_sha256}; {pane_result})"
    )
    result = f"{updated}\n\n{note}\n"
    metadata = parse_task_metadata(result, root)
    if metadata is None or metadata.status != "done" or metadata.pending_task_items:
        raise TaskFrontmatterError("closure did not produce a terminal empty task record.")
    return result


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def decoded(value: object) -> str:
    if not isinstance(value, str):
        raise TaskFrontmatterError("packet text is malformed.")
    try:
        return base64.b64decode(value, validate=True).decode()
    except Exception as exc:
        raise TaskFrontmatterError("packet text is not canonical base64 UTF-8.") from exc


def rename_exchange(parent_fd: int, left: str, right: str) -> None:
    function = ctypes.CDLL(None, use_errno=True).renameat2
    function.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(parent_fd, os.fsencode(left), parent_fd, os.fsencode(right), 2) == 0:
        return
    error = ctypes.get_errno()
    raise TaskFrontmatterError(f"atomic descriptor-bound exchange failed: {os.strerror(error)}")


def held_values(state: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.S_IMODE(state.st_mode), state.st_dev, state.st_ino,
        state.st_uid, state.st_gid, state.st_size,
    )


def same_open_leaf(parent_fd: int, name: str, descriptor: int) -> bool:
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return held_values(linked) == held_values(os.fstat(descriptor))


def replace_held(held: HeldAbsolute, text: str) -> os.stat_result:
    """Exchange through the held parent, then prove the displaced leaf was exact."""

    parent_fd = held.directories[-1]
    name = f".{held.leaf_name}.{secrets.token_hex(16)}"
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, held.identity.mode, dir_fd=parent_fd)
    exchanged = False
    try:
        payload = text.encode()
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise TaskFrontmatterError("descriptor-bound replacement made no write progress.")
            remaining = remaining[written:]
        os.fsync(fd)
        os.fchmod(fd, held.identity.mode)
        validate_held_absolute(held)
        rename_exchange(parent_fd, name, held.leaf_name)
        exchanged = True
        displaced = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        expected = held.identity
        expected_values = (expected.mode, expected.device, expected.inode, expected.uid, expected.gid, expected.size_bytes)
        if held_values(displaced) != expected_values or displaced.st_nlink != 1:
            rename_exchange(parent_fd, name, held.leaf_name)
            exchanged = False
            raise TaskFrontmatterError("final-window leaf substitution was detected and restored.")
        try:
            published_fd = os.open(held.leaf_name, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                published = os.fstat(published_fd)
                linked = os.stat(held.leaf_name, dir_fd=parent_fd, follow_symlinks=False)
                output = bytearray()
                while chunk := os.read(published_fd, 64 * 1024):
                    output.extend(chunk)
            finally:
                os.close(published_fd)
            if held_values(published) != held_values(linked) or not stat.S_ISREG(published.st_mode) or sha256(payload) != sha256(bytes(output)):
                raise TaskFrontmatterError("descriptor-bound replacement did not publish the exact bytes.")
            os.fsync(parent_fd)
            os.unlink(name, dir_fd=parent_fd)
            exchanged = False
            return published
        except Exception as exc:
            if same_open_leaf(parent_fd, held.leaf_name, fd):
                try:
                    rename_exchange(parent_fd, name, held.leaf_name)
                    exchanged = False
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except Exception as restore_error:
                    raise IndeterminateClose(
                        f"indeterminate descriptor-bound replacement; displaced leaf remains at {name}: {restore_error}"
                    ) from exc
                raise TaskFrontmatterError("descriptor-bound replacement failed and restored the exact prior leaf.") from exc
            raise IndeterminateClose(
                f"indeterminate descriptor-bound replacement; displaced exact leaf remains at {name}"
            ) from exc
    finally:
        os.close(fd)
        if not exchanged:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    raise


def require_private_output(path: Path, inputs: set[Path]) -> Path:
    if not path.is_absolute() or path.resolve() in inputs:
        raise TaskFrontmatterError("packet and audit outputs must be absolute and distinct from bound inputs.")
    parent = path.parent.resolve(strict=True)
    state = parent.stat()
    if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) & 0o077:
        raise TaskFrontmatterError("packet and audit output directories must be owner-private.")
    return path.resolve()


def restore_exact_after(path: Path, expected_after_sha256: str, before_text: str) -> None:
    """Restore one transaction leaf only while its exact after-image remains linked."""

    data, identity, ancestors = absolute_file_binding(path, f"closure rollback input {path}")
    if sha256(data) != expected_after_sha256:
        raise IndeterminateClose(f"closure rollback refused changed after-image: {path}")
    held = hold_absolute(identity, ancestors)
    try:
        replace_held(held, before_text)
    except Exception as exc:
        raise IndeterminateClose(f"closure rollback is incomplete for {path}: {exc}") from exc
    finally:
        os.close(held.descriptor)
        for descriptor in reversed(held.directories):
            os.close(descriptor)


def direct_children(root: Path, task: Path, target: str) -> list[dict[str, object]]:
    """Return the exact byte and lifecycle identity of every direct child."""

    result = []
    for candidate in root.rglob("*.md"):
        if "manager_mail" in candidate.parts or candidate == task:
            continue
        data, _ = read_regular_unbound(candidate)
        text = data.decode()
        try:
            metadata = parse_task_metadata(text, root)
        except TaskFrontmatterError as error:
            try:
                source = frontmatter_text(text)
            except TaskFrontmatterError:
                if re.search(r"^\s*managerat\s*:", text, re.MULTILINE):
                    raise error
                continue
            try:
                values = yaml.load(source, Loader=UniqueKeyLoader) if source is not None else None
            except (TaskFrontmatterError, TypeError, ValueError, yaml.YAMLError):
                if source is not None and re.search(r"^\s*managerat\s*:", source, re.MULTILINE):
                    raise error
                continue
            if isinstance(values, Mapping) and values.get("managerat") == target:
                raise error
            continue
        if metadata is not None and metadata.managerat == target:
            result.append({
                "task": relative_task_ref(root, candidate),
                "sha256": sha256(data),
                "status": metadata.status,
                "pending_items": len(metadata.pending_task_items),
            })
    return sorted(result, key=lambda item: item["task"])


def prepare(ns: argparse.Namespace) -> None:
    root = ns.root.resolve(strict=True)
    task = (root / ns.task).resolve(strict=True)
    try:
        task.relative_to(root)
        authority_relative = ns.authority.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise TaskFrontmatterError("task and authority must stay inside the task root.") from exc
    if len(authority_relative.parts) != 2 or authority_relative.parts[0] != "manager_mail":
        raise TaskFrontmatterError("authority must be one direct manager_mail file.")
    todo = root / "TODO.md"
    envelope = (root / ns.authority_envelope).resolve(strict=True)
    lock_paths = {task, todo, ns.export.resolve(strict=True), ns.authority.resolve(strict=True), envelope}
    if ns.protected_task is not None:
        protected_path = (root / ns.protected_task).resolve(strict=True)
        try:
            protected_path.relative_to(root)
        except ValueError as exc:
            raise TaskFrontmatterError("protected sibling must stay inside the task root.") from exc
        lock_paths.add(protected_path)
    with root_membership_lock(root):
        if ns.mode == "live-manager-terminal-children":
            ns.bound_children = direct_children(root, task, ns.target)
            lock_paths.update(root / child["task"] for child in ns.bound_children)
        else:
            ns.bound_children = []
        ns.packet = require_private_output(ns.packet, lock_paths)
        ns.audit = require_private_output(ns.audit, lock_paths | {ns.packet})
        prepare_with_membership_lock(ns, root, task, todo, lock_paths)


def prepare_with_membership_lock(
    ns: argparse.Namespace, root: Path, task: Path, todo: Path, lock_paths: set[Path]
) -> None:
    """Prepare while the caller retains exclusive task-root membership."""

    with task_target_lock(root, ns.target), ExitStack() as locks:
        for path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(path))
        prepare_locked(ns, root, task, todo)


def prepare_locked(ns: argparse.Namespace, root: Path, task: Path, todo: Path) -> None:
    """Build a packet while all mutable inputs and target membership are locked."""

    task_data, _ = read_regular(task, ns.task_sha256)
    todo_data, _ = read_regular(todo, ns.todo_sha256)
    export_data, _ = read_regular(ns.export.resolve(strict=True), ns.export_sha256)
    read_regular(ns.authority.resolve(strict=True), ns.authority_sha256)
    task_text, todo_text, export_text = (data.decode() for data in (task_data, todo_data, export_data))
    metadata = parse_task_metadata(task_text, root)
    if metadata is None or metadata.version != "v1.0.0" or has_pending_marker(task_text):
        raise TaskFrontmatterError("exported closure requires one canonical v1 task without a pending marker.")
    status_args = StatusArgs(
        root,
        ns.task,
        "done",
        "",
        authority_file=ns.authority,
        authority_lines=ns.authority_lines,
        authority_sha256=ns.authority_sha256,
        authority_envelope=ns.authority_envelope,
        authority_envelope_sha256=ns.authority_envelope_sha256,
    )
    excerpt, locator = read_park_authority(status_args)
    envelope_identity = read_park_authority_envelope(status_args, excerpt, locator)
    if not has_export_then_close_authority(excerpt):
        raise TaskFrontmatterError("authority does not contain the complete export-then-close procedure.")
    validate_export(export_text, relative_task_ref(root, task), ns.task_sha256, metadata)
    if metadata.runat != ns.target:
        raise TaskFrontmatterError("task target does not match the requested target.")
    pane_id = park_target_pane_id(ns.target)
    protected: dict[str, str] | None = None
    pane_pid = 0
    pane_start_ticks = 0
    session_id = ""
    children: list[dict[str, object]] = []
    if ns.mode == "absent-manager-previous":
        if metadata.status != "long_running" or not metadata.is_manager or pane_id:
            raise TaskFrontmatterError("absent-manager closure shape does not match.")
    elif ns.mode == "absent-worker-unindexed":
        if metadata.status != "blocked" or metadata.is_manager or pane_id:
            raise TaskFrontmatterError("absent-worker closure shape does not match.")
    elif ns.mode == "live-manager-terminal-children":
        if metadata.status != "blocked" or not metadata.is_manager or metadata.pending_task_items or pane_id != ns.pane_id:
            raise TaskFrontmatterError("live terminal-child manager closure shape does not match.")
        if metadata.session_id != ns.session_id or not ns.session_id:
            raise TaskFrontmatterError("live manager session identity does not match.")
        identity = target_identity(ns.target, inspect_target(ns.target))
        if identity != (ns.pane_id, ns.pane_pid, ns.pane_start_ticks):
            raise TaskFrontmatterError("live manager pane process identity does not match.")
        children = direct_children(root, task, ns.target)
        if children != ns.bound_children:
            raise TaskFrontmatterError("live manager child identity changed before packet binding.")
        if any(child["status"] != "done" or child["pending_items"] != 0 for child in children):
            raise TaskFrontmatterError("live manager still has nonterminal children.")
        pane_pid, pane_start_ticks, session_id = ns.pane_pid, ns.pane_start_ticks, ns.session_id
    elif ns.mode == "shared-live-worker":
        if metadata.status != "blocked" or metadata.is_manager or not pane_id or pane_id != ns.pane_id:
            raise TaskFrontmatterError("shared-live worker closure shape does not match.")
    else:
        if metadata.status != "blocked" or not metadata.is_manager or pane_id or ns.pane_id:
            raise TaskFrontmatterError("shared-absent manager closure shape does not match.")
    if ns.mode in {"shared-absent-manager", "shared-live-worker"}:
        if ns.protected_task is None or not ns.protected_sha256:
            raise TaskFrontmatterError("shared closure requires one exact protected sibling.")
        protected_path = (root / ns.protected_task).resolve(strict=True)
        if protected_path == task:
            raise TaskFrontmatterError("shared closure protected sibling must differ from the source task.")
        protected_data, _ = read_regular(protected_path, ns.protected_sha256)
        owners = {relative_task_ref(root, path) for path in authoritative_active_target_task_paths(root, ns.target)}
        if owners != {relative_task_ref(root, task), relative_task_ref(root, protected_path)}:
            raise TaskFrontmatterError("shared target does not have exactly the bound two owners.")
        protected = {"task": relative_task_ref(root, protected_path), "sha256": sha256(protected_data)}
    after_task = closed_task(root, task, task_text, ns.export.resolve(), ns.export_sha256, ns.mode)
    after_todo = closed_todo(root, task, todo_text, ns.mode, ns.target)
    envelope_path = (root / ns.authority_envelope).resolve()
    input_paths = [task, todo, ns.export.resolve(), ns.authority.resolve(), envelope_path]
    if protected is not None:
        input_paths.append(root / protected["task"])
    input_paths.extend(root / child["task"] for child in children)
    inputs = []
    for input_path in input_paths:
        _, identity, ancestors = absolute_file_binding(input_path.resolve(), f"closure input {input_path}")
        if input_path == envelope_path and identity.sha256 != ns.authority_envelope_sha256:
            raise TaskFrontmatterError("authority envelope changed between semantic validation and identity binding.")
        inputs.append({"file": asdict(identity), "ancestors": [asdict(item) for item in ancestors]})
    packet = {
        "schema": SCHEMA,
        "mode": ns.mode,
        "root": str(root),
        "task": relative_task_ref(root, task),
        "target": ns.target,
        "pane_id": pane_id,
        "task_before_sha256": ns.task_sha256,
        "todo_before_sha256": ns.todo_sha256,
        "task_after_sha256": sha256(after_task.encode()),
        "todo_after_sha256": sha256(after_todo.encode()),
        "task_after_base64": encoded(after_task),
        "todo_after_base64": encoded(after_todo),
        "export": str(ns.export.resolve()),
        "export_sha256": ns.export_sha256,
        "authority": str(ns.authority.resolve()),
        "authority_sha256": ns.authority_sha256,
        "authority_envelope": envelope_identity,
        "authority_envelope_sha256": ns.authority_envelope_sha256,
        "protected": protected,
        "pane_pid": pane_pid,
        "pane_start_ticks": pane_start_ticks,
        "session_id": session_id,
        "children": children,
        "audit": str(ns.audit.resolve()),
        "destination_target": ns.destination_target,
        "inputs": inputs,
    }
    packet["binding_id"] = sha256(json.dumps(packet, sort_keys=True, separators=(",", ":")).encode())
    packet_data = (json.dumps(packet, sort_keys=True, separators=(",", ":")) + "\n").encode()
    publish_or_validate(ns.packet.resolve(), packet_data, "exported-agent closure packet")
    print(ns.packet.resolve())


def execute(ns: argparse.Namespace) -> None:
    packet_data, _ = read_regular(ns.packet.resolve(strict=True), ns.packet_sha256)
    review_data, _ = read_regular(ns.review.resolve(strict=True), ns.review_sha256)
    packet = json.loads(packet_data)
    if (
        not isinstance(packet, dict)
        or set(packet) != PACKET_KEYS
        or packet.get("schema") != SCHEMA
        or packet.get("mode") not in MODES
    ):
        raise TaskFrontmatterError("closure packet is malformed.")
    identity = dict(packet)
    binding_id = identity.pop("binding_id")
    if binding_id != sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()):
        raise TaskFrontmatterError("closure packet binding identity is invalid.")
    review_report = authenticated_report(review_data, "exported-agent closure review")
    try:
        review = json.loads(review_data.split(b"message:\n", 1)[1])
    except (IndexError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TaskFrontmatterError("authenticated review body is not JSON.") from exc
    if review != {"schema": REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": ns.packet_sha256}:
        raise TaskFrontmatterError("independent review does not PASS this exact packet.")
    if review_report["producer_target"] in {packet["target"], packet["destination_target"]}:
        raise TaskFrontmatterError("review producer is not independent of source and destination.")
    root = Path(packet["root"])
    task = root / packet["task"]
    todo = root / "TODO.md"
    export = Path(packet["export"])
    authority = Path(packet["authority"])
    protected = packet.get("protected")
    audit = {
        key: packet[key]
        for key in (
            "schema", "mode", "task", "target", "pane_id", "task_before_sha256",
            "todo_before_sha256", "task_after_sha256", "todo_after_sha256", "export",
            "export_sha256", "authority", "authority_sha256", "protected", "binding_id",
            "pane_pid", "pane_start_ticks", "session_id", "children",
        )
    }
    prepared_data = (json.dumps({**audit, "state": "prepared"}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    audit_data = (json.dumps({**audit, "state": "committed"}, sort_keys=True, separators=(",", ":")) + "\n").encode()
    prepared_path = Path(f"{packet['audit']}.prepared")
    prepared_exists = existing_exact(prepared_path, prepared_data, "prepared closure audit") if prepared_path.exists() else False
    authority_envelope = root / packet["authority_envelope"]
    lock_paths = {task, todo, export, authority, authority_envelope}
    if isinstance(protected, dict):
        lock_paths.add(root / protected["task"])
    children = packet.get("children")
    if not isinstance(children, list) or any(
        not isinstance(child, dict)
        or set(child) != {"task", "sha256", "status", "pending_items"}
        or not isinstance(child["task"], str)
        or not isinstance(child["sha256"], str)
        or not isinstance(child["status"], str)
        or not isinstance(child["pending_items"], int)
        for child in children
    ):
        raise TaskFrontmatterError("closure packet child manifest is malformed.")
    if packet["mode"] != "live-manager-terminal-children" and children:
        raise TaskFrontmatterError("closure packet unexpectedly binds child tasks.")
    for child in children:
        child_path = (root / child["task"]).resolve(strict=True)
        try:
            child_path.relative_to(root)
        except ValueError as exc:
            raise TaskFrontmatterError("closure packet child task escapes the task root.") from exc
        if not SHA256_RE.fullmatch(child["sha256"]):
            raise TaskFrontmatterError("closure packet child digest is malformed.")
        lock_paths.add(child_path)
    with root_membership_lock(root), task_target_lock(root, packet["target"]), ExitStack() as locks:
        for path in sorted(lock_paths, key=str):
            locks.enter_context(task_file_lock(path))
        held_inputs = []
        raw_inputs = packet.get("inputs")
        if not isinstance(raw_inputs, list) or len(raw_inputs) != len(lock_paths):
            raise TaskFrontmatterError("packet input identity set is incomplete.")
        for raw_input in raw_inputs:
            if not isinstance(raw_input, dict) or set(raw_input) != {"file", "ancestors"} or not isinstance(raw_input["ancestors"], list):
                raise TaskFrontmatterError("packet input identity is malformed.")
            identity = file_identity_from(raw_input["file"], "closure input")
            ancestors = tuple(directory_identity_from(item, "closure input ancestor") for item in raw_input["ancestors"])
            if prepared_exists and Path(identity.path) in {task, todo}:
                current_data, current_identity, current_ancestors = absolute_file_binding(Path(identity.path), "recoverable closure input")
                after_sha = packet["task_after_sha256"] if Path(identity.path) == task else packet["todo_after_sha256"]
                if sha256(current_data) in {identity.sha256, after_sha}:
                    identity, ancestors = current_identity, current_ancestors
            held = hold_absolute(identity, ancestors)
            held_inputs.append(held)
            locks.callback(os.close, held.descriptor)
            for descriptor in reversed(held.directories):
                locks.callback(os.close, descriptor)
        if {Path(held.identity.path) for held in held_inputs} != {path.resolve() for path in lock_paths}:
            raise TaskFrontmatterError("packet input identity set does not match the operation inputs.")
        for held in held_inputs:
            validate_held_absolute(held)
        task_data, task_state = read_regular_unbound(task)
        todo_data, todo_state = read_regular_unbound(todo)
        task_sha = sha256(task_data)
        todo_sha = sha256(todo_data)
        before_pair = (packet["task_before_sha256"], packet["todo_before_sha256"])
        after_pair = (packet["task_after_sha256"], packet["todo_after_sha256"])
        if (task_sha, todo_sha) not in {before_pair, (before_pair[0], after_pair[1]), after_pair}:
            raise TaskFrontmatterError("task/TODO state is neither initial, recoverable partial, nor committed.")
        read_regular(export, packet["export_sha256"])
        read_regular(authority, packet["authority_sha256"])
        if isinstance(protected, dict):
            read_regular(root / protected["task"], protected["sha256"])
        pane_id = park_target_pane_id(packet["target"])
        if packet["mode"] == "live-manager-terminal-children":
            live_identity = target_identity(packet["target"], inspect_target(packet["target"]))
            if live_identity != (packet["pane_id"], packet["pane_pid"], packet["pane_start_ticks"]) and not (
                prepared_exists and live_identity == ("", 0, 0)
            ):
                raise TaskFrontmatterError("live manager pane identity drifted.")
            current_children = direct_children(root, task, packet["target"])
            if current_children != children:
                raise TaskFrontmatterError("live manager child identity drifted.")
            if any(
                child["status"] != "done" or child["pending_items"] != 0
                for child in current_children
            ):
                raise TaskFrontmatterError("live manager gained a nonterminal child.")
        elif packet["mode"] in {"shared-absent-manager", "shared-live-worker"}:
            owners = {relative_task_ref(root, path) for path in authoritative_active_target_task_paths(root, packet["target"])}
            expected_owners = (
                {packet["task"], protected["task"]}
                if task_sha == packet["task_before_sha256"]
                else {protected["task"]}
            )
            if pane_id != packet["pane_id"] or owners != expected_owners:
                raise TaskFrontmatterError("shared target or sibling ownership drifted.")
        elif pane_id:
            raise TaskFrontmatterError("absent target became live.")
        after_task = decoded(packet["task_after_base64"])
        after_todo = decoded(packet["todo_after_base64"])
        if sha256(after_task.encode()) != packet["task_after_sha256"] or sha256(after_todo.encode()) != packet["todo_after_sha256"]:
            raise TaskFrontmatterError("closure packet after-bytes are inconsistent.")
        publish_or_validate(prepared_path, prepared_data, "prepared closure audit")
        if Path(packet["audit"]).exists():
            existing, _ = read_regular(Path(packet["audit"]), sha256(audit_data))
            if existing != audit_data:
                raise TaskFrontmatterError("closure audit output is already occupied.")
            return
        if packet["mode"] == "live-manager-terminal-children":
            if park_target_pane_id(packet["target"]):
                stop_target(
                    packet["target"], packet["pane_id"], packet["pane_pid"],
                    packet["pane_start_ticks"], packet["session_id"],
                )
            if park_target_pane_id(packet["target"]):
                raise IndeterminateClose("live manager pane remains after reviewed close attempt.")
        held_by_path = {Path(held.identity.path): held for held in held_inputs}
        if todo_sha == packet["todo_before_sha256"]:
            replace_held(held_by_path[todo], after_todo)
        try:
            if task_sha == packet["task_before_sha256"]:
                replace_held(held_by_path[task], after_task)
        except Exception as exc:
            if todo_sha == packet["todo_before_sha256"] and not isinstance(exc, IndeterminateClose):
                _, rollback_identity, rollback_ancestors = absolute_file_binding(todo, "TODO rollback input")
                rollback_hold = hold_absolute(rollback_identity, rollback_ancestors)
                try:
                    replace_held(rollback_hold, todo_data.decode())
                finally:
                    os.close(rollback_hold.descriptor)
                    for descriptor in reversed(rollback_hold.directories):
                        os.close(descriptor)
            if isinstance(exc, IndeterminateClose):
                raise
            raise TaskFrontmatterError(f"task update failed after TODO update; rollback completed: {exc}") from exc
        verification_error = ""
        if packet["mode"] == "live-manager-terminal-children":
            if park_target_pane_id(packet["target"]):
                verification_error = "live manager target reappeared after closure."
        elif packet["mode"] in {"shared-absent-manager", "shared-live-worker"}:
            owners = {relative_task_ref(root, path) for path in authoritative_active_target_task_paths(root, packet["target"])}
            if park_target_pane_id(packet["target"]) != packet["pane_id"] or owners != {protected["task"]}:
                verification_error = "shared target or surviving ownership changed after metadata-only closure."
        elif park_target_pane_id(packet["target"]):
            verification_error = "absent target became live after metadata-only closure."
        if verification_error:
            restore_exact_after(task, packet["task_after_sha256"], task_data.decode())
            restore_exact_after(todo, packet["todo_after_sha256"], todo_data.decode())
            raise TaskFrontmatterError(f"{verification_error} Exact task and TODO bytes were restored.")
        publish_or_validate(Path(packet["audit"]), audit_data, "committed closure audit")
    print(Path(packet["audit"]))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, required=True)
    prepare_parser.add_argument("--task", type=Path, required=True)
    prepare_parser.add_argument("--mode", choices=sorted(MODES), required=True)
    prepare_parser.add_argument("--target", required=True)
    prepare_parser.add_argument("--task-sha256", required=True)
    prepare_parser.add_argument("--todo-sha256", required=True)
    prepare_parser.add_argument("--export", type=Path, required=True)
    prepare_parser.add_argument("--export-sha256", required=True)
    prepare_parser.add_argument("--authority", type=Path, required=True)
    prepare_parser.add_argument("--authority-sha256", required=True)
    prepare_parser.add_argument("--authority-lines", type=lambda value: tuple(map(int, value.split(":"))), required=True)
    prepare_parser.add_argument("--authority-envelope", type=Path, required=True)
    prepare_parser.add_argument("--authority-envelope-sha256", required=True)
    prepare_parser.add_argument("--destination-target", required=True)
    prepare_parser.add_argument("--protected-task", type=Path)
    prepare_parser.add_argument("--protected-sha256", default="")
    prepare_parser.add_argument("--pane-id", default="")
    prepare_parser.add_argument("--pane-pid", type=int, default=0)
    prepare_parser.add_argument("--pane-start-ticks", type=int, default=0)
    prepare_parser.add_argument("--session-id", default="")
    prepare_parser.add_argument("--audit", type=Path, required=True)
    prepare_parser.add_argument("--packet", type=Path, required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--packet", type=Path, required=True)
    execute_parser.add_argument("--packet-sha256", required=True)
    execute_parser.add_argument("--review", type=Path, required=True)
    execute_parser.add_argument("--review-sha256", required=True)
    return result


def main() -> int:
    try:
        ns = parser().parse_args()
        values = vars(ns)
        for key, value in values.items():
            if key.endswith("sha256") and value and SHA256_RE.fullmatch(value) is None:
                raise TaskFrontmatterError(f"--{key.replace('_', '-')} must be lowercase SHA-256.")
        (prepare if ns.command == "prepare" else execute)(ns)
    except (TaskFrontmatterError, CustodyError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"omo_exported_agent_close.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
