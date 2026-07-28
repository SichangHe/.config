#!/usr/bin/env python3
"""Commit one authenticated agent report and emit its durable receipt."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .omo_pending_digest import PENDING_CONTENT_CHAR_LIMIT
from .omo_task_lock import task_file_lock_at_path
from .omo_task_lock import task_file_lock_path
from .omo_task_lock import watcher_report_authority_is_live
from .omo_task_lock import watcher_report_manager_temporary
from .omo_task_lock import watcher_report_state_maintenance_temporary
from .omo_task_lock import watcher_report_state_temporary


RECEIVER_VERSION = "4"
DESCRIPTION_SCHEMA = "omo-report-description/v1"
ACCEPTANCE_SCHEMA = "omo-report-acceptance/v1"
RECEIPT_SCHEMA = "omo-report-receipt/v1"
RECEIPT_PUBLICATION_SCHEMA = "omo-report-receipt-publication/v1"
TRANSACTION_COMMITMENT_SCHEMA = "omo-report-transaction-commitment/v1"
BINDING_SCHEMA = "omo-report-binding/v1"
MAX_ENVELOPE_BYTES = PENDING_CONTENT_CHAR_LIMIT * 8
MAX_PROGRAM_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_ROUTE_FILE_BYTES = 64 * 1024 * 1024
MAX_ACK_STATE_BYTES = 4 * 1024 * 1024
DEFAULT_ACK_TIMEOUT_S = 3.0
AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ROUTE_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
TARGET_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(\d+)(?:\.(\d+))?$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PANE_ID_RE = re.compile(r"^%[A-Za-z0-9_.-]+$")
REPORT_CONTEXT_RE = re.compile(r"^(batch|attempt): ([A-Za-z0-9._-]+)$")
SENT_LINE_RE = re.compile(
    r"^\(sent from ([A-Za-z0-9_.-]+) via omo_report\.sh tmux=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) "
    r"time=\S+ task-file=([A-Za-z0-9_.-]+)\)$"
)
HASH_LINE_RE = re.compile(r"^\[message-sha256: ([0-9a-f]{64})\]$")
OWNER_PREFIX_LINE_RE = re.compile(
    r"^\[omo-report-owner-prefix: manager-path-sha256=([0-9a-f]{64}) sha256=([0-9a-f]{64}) "
    r"size-bytes=(0|[1-9][0-9]*) separator-bytes=([12])\]$"
)
VOLATILE_REPLAY_ROUTING_KEYS = frozenset({"route_evidence", "route_evidence_sha256", "route_local_date", "tmux"})


class ReceiptError(RuntimeError):
    """A fail-closed report transaction error."""


@dataclass(frozen=True)
class OwnerPrefixBinding:
    manager_path_sha256: str
    sha256: str
    size_bytes: int
    separator_bytes: int


@dataclass(frozen=True)
class Arguments:
    mode: str
    helper: Path
    root: Path
    task: Path
    manager: Path
    requested_manager_target: str
    resolved_manager_target: str
    route_kind: str
    route_note: str
    task_route_evidence: str
    manager_route_evidence: str
    route_local_date: str
    status: str
    message_file: Path
    agent: str
    producer_target: str
    tmux_session: str
    tmux_window_index: str
    tmux_pane_index: str
    tmux_pane_id: str
    tmux_window_name: str


@dataclass(frozen=True)
class Plan:
    mode: str
    root: Path
    task: Path
    manager: Path
    helper_path: Path
    receiver_path: Path
    message_path: Path
    message_identity: tuple[int, int]
    message_fd: int
    message: bytes
    status: str
    input_info: dict[str, object]
    report_context: dict[str, object]
    routing: dict[str, object]
    route_evidence: tuple[dict[str, object], ...]
    route_locks: tuple[tuple[Path, Path], ...]
    helper: dict[str, object]
    replay_id: str
    owner_prefix: OwnerPrefixBinding
    report_lock: Path
    task_lock: Path
    manager_temporary: Path
    manager_watcher_temporary: Path
    envelope_directory: Path
    envelope_temporary: Path
    envelope_final: Path
    pointer: str
    acknowledgment_state: Path
    acknowledgment_lock: Path
    acknowledgment_temporary: Path
    acknowledgment_maintenance_temporary: Path
    acknowledgment_key: str
    acknowledgment_authority_lock: Path
    acknowledgment_authority_completion: Path
    receipt_directory: Path
    transaction_commitment_temporary: Path
    transaction_commitment_final: Path
    receipt_temporary: Path
    receipt_final: Path
    receipt_publication_temporary: Path
    receipt_publication_final: Path


def parse_args(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--mode", required=True, choices=("describe", "submit"))
    _ = parser.add_argument("--helper", required=True, type=Path)
    _ = parser.add_argument("--root", required=True, type=Path)
    _ = parser.add_argument("--task", required=True, type=Path)
    _ = parser.add_argument("--manager", required=True, type=Path)
    _ = parser.add_argument("--requested-manager-target", required=True)
    _ = parser.add_argument("--resolved-manager-target", required=True)
    _ = parser.add_argument("--route-kind", required=True)
    _ = parser.add_argument("--route-note", required=True)
    _ = parser.add_argument("--task-route-evidence", required=True)
    _ = parser.add_argument("--manager-route-evidence", required=True)
    _ = parser.add_argument("--route-local-date", required=True)
    _ = parser.add_argument("--status", required=True)
    _ = parser.add_argument("--message-file", required=True, type=Path)
    _ = parser.add_argument("--agent", required=True)
    _ = parser.add_argument("--producer-target", required=True)
    _ = parser.add_argument("--tmux-session", default="")
    _ = parser.add_argument("--tmux-window-index", default="")
    _ = parser.add_argument("--tmux-pane-index", default="")
    _ = parser.add_argument("--tmux-pane-id", default="")
    _ = parser.add_argument("--tmux-window-name", default="")
    parsed = parser.parse_args(argv)
    return Arguments(
        parsed.mode,
        parsed.helper,
        parsed.root,
        parsed.task,
        parsed.manager,
        parsed.requested_manager_target,
        parsed.resolved_manager_target,
        parsed.route_kind,
        parsed.route_note,
        parsed.task_route_evidence,
        parsed.manager_route_evidence,
        parsed.route_local_date,
        parsed.status,
        parsed.message_file,
        parsed.agent,
        parsed.producer_target,
        parsed.tmux_session,
        parsed.tmux_window_index,
        parsed.tmux_pane_index,
        parsed.tmux_pane_id,
        parsed.tmux_window_name,
    )


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def replay_routing_identity(routing: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in routing.items() if key not in VOLATILE_REPLAY_ROUTING_KEYS}


def bound_receipt_id(receipt_without_id: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json(receipt_without_id).rstrip(b"\n")).hexdigest()


def absolute_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def normalize_status(value: str) -> str:
    normalized = re.sub(r"[_\s]+", "-", value.strip().casefold())
    normalized = re.sub(r"-+", "-", normalized)
    normalized = {"progressing": "in-progress"}.get(normalized, normalized)
    if normalized not in {"blocked", "in-progress", "done"}:
        raise ReceiptError("status must be blocked, progressing, in-progress, or done")
    return normalized


def canonical_target(value: str, *, required: bool, field: str) -> str:
    if not value:
        if required:
            raise ReceiptError(f"{field} must be a tmux target")
        return ""
    match = TARGET_RE.fullmatch(value)
    if match is None:
        raise ReceiptError(f"{field} must be a tmux target")
    session, window_text, pane_text = match.groups()
    window = int(window_text)
    pane = int(pane_text or "0")
    return f"{session}:{window}" if pane == 0 else f"{session}:{window}.{pane}"


def validate_text_field(value: str, field: str, *, max_length: int, allow_empty: bool = True) -> None:
    if not allow_empty and not value:
        raise ReceiptError(f"{field} must not be empty")
    if len(value) > max_length or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ReceiptError(f"{field} contains invalid text")


def open_regular_file_snapshot(
    path: Path,
    *,
    maximum: int,
    field: str,
    require_owner: bool = True,
) -> tuple[bytes, tuple[int, int], int]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError(f"{field} is not a readable regular file") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (require_owner and before.st_uid != os.getuid()):
            raise ReceiptError(f"{field} is not an owned regular file")
        if before.st_size > maximum:
            raise ReceiptError(f"{field} exceeds its size limit")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after or len(payload) != before.st_size:
            raise ReceiptError(f"{field} changed while it was read")
        if len(payload) > maximum:
            raise ReceiptError(f"{field} exceeds its size limit")
        return payload, (before.st_dev, before.st_ino), fd
    except BaseException:
        os.close(fd)
        raise


def regular_file_snapshot(
    path: Path,
    *,
    maximum: int,
    field: str,
    require_owner: bool = True,
) -> tuple[bytes, tuple[int, int]]:
    payload, identity, fd = open_regular_file_snapshot(
        path,
        maximum=maximum,
        field=field,
        require_owner=require_owner,
    )
    try:
        return payload, identity
    finally:
        os.close(fd)


def regular_file_bytes(path: Path, *, maximum: int, field: str, require_owner: bool = True) -> bytes:
    payload, _ = regular_file_snapshot(path, maximum=maximum, field=field, require_owner=require_owner)
    return payload


def validate_directory(path: Path, *, private: bool, field: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ReceiptError(f"{field} is not a directory") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ReceiptError(f"{field} is not an owned real directory")
    if path.resolve(strict=True) != path:
        raise ReceiptError(f"{field} resolves through a symlink")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        raise ReceiptError(f"{field} must have mode 0700")
    return info


def validate_optional_regular(path: Path, field: str, *, exact_mode: int | None = None) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReceiptError(f"cannot inspect {field}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ReceiptError(f"{field} is not an owned regular file")
    if exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode:
        raise ReceiptError(f"{field} has an invalid mode")
    return True


def validate_optional_directory(path: Path, *, private: bool, field: str) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ReceiptError(f"cannot inspect {field}") from exc
    _ = validate_directory(path, private=private, field=field)
    return True


def safe_part(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"


def safe_label(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9:._%-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"


def extract_report_context(message: str) -> dict[str, object]:
    values: dict[str, str] = {}
    for line in message.split("\n"):
        match = REPORT_CONTEXT_RE.fullmatch(line)
        if match is None:
            continue
        key, value = match.groups()
        if key in values:
            raise ReceiptError(f"duplicate {key} report context lines")
        values[key] = value
    return {"attempt": values.get("attempt"), "batch": values.get("batch")}


def tmux_metadata(args: Arguments, producer_target: str) -> dict[str, object]:
    raw = {
        "pane_id": args.tmux_pane_id,
        "pane_index": args.tmux_pane_index,
        "session": args.tmux_session,
        "window_index": args.tmux_window_index,
        "window_name": args.tmux_window_name,
    }
    supplied: dict[str, object] = {key: value for key, value in raw.items() if value}
    identity_values = (args.tmux_session, args.tmux_window_index, args.tmux_pane_index)
    if any(identity_values) and not all(identity_values):
        raise ReceiptError("tmux identity fields are incomplete")
    if all(identity_values):
        candidate = canonical_target(
            f"{args.tmux_session}:{args.tmux_window_index}.{args.tmux_pane_index}",
            required=True,
            field="tmux identity",
        )
        if candidate != producer_target:
            raise ReceiptError("tmux identity does not match producer target")
    if args.tmux_pane_id and PANE_ID_RE.fullmatch(args.tmux_pane_id) is None:
        raise ReceiptError("tmux pane id is invalid")
    for key, value in raw.items():
        validate_text_field(value, f"tmux {key}", max_length=256)
    return supplied


def receipt_state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ReceiptError("XDG_STATE_HOME must be absolute")
        return path.resolve(strict=False)
    return (Path.home() / ".local" / "state").resolve(strict=False)


def manager_acknowledgment_key(root: Path, envelope: Path, input_sha256: str) -> str:
    hash_line = f"[message-sha256: {input_sha256}]"
    source = str(envelope)
    identity = f"{source}\0{source}\0{hash_line}"
    return f"{root}:agent-report:{hashlib.sha256(identity.encode()).hexdigest()}"


def parse_route_evidence(
    raw_manifests: tuple[str, str],
    *,
    root: Path,
    task: Path,
    manager: Path,
    route_kind: str,
) -> tuple[dict[str, object], ...]:
    merged: dict[Path, dict[str, object]] = {}
    for raw_manifest in raw_manifests:
        try:
            manifest = json.loads(raw_manifest)
        except json.JSONDecodeError as exc:
            raise ReceiptError("route evidence is not valid JSON") from exc
        if not isinstance(manifest, list) or not manifest:
            raise ReceiptError("route evidence is incomplete")
        seen: set[Path] = set()
        for value in manifest:
            if not isinstance(value, dict):
                raise ReceiptError("route evidence entry is invalid")
            exists = value.get("exists")
            expected_keys = {"exists", "path", "sha256", "size_bytes"} if exists is True else {"exists", "path"}
            if exists not in {True, False} or set(value) != expected_keys:
                raise ReceiptError("route evidence entry is invalid")
            raw_path = value.get("path")
            if not isinstance(raw_path, str):
                raise ReceiptError("route evidence path is invalid")
            path = Path(raw_path)
            if not path.is_absolute() or path != path.absolute():
                raise ReceiptError("route evidence path must be absolute")
            if path in seen:
                raise ReceiptError("route evidence contains a duplicate path")
            seen.add(path)
            if exists:
                digest = value.get("sha256")
                size_bytes = value.get("size_bytes")
                if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
                    raise ReceiptError("route evidence digest is invalid")
                if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or not 0 <= size_bytes <= MAX_ROUTE_FILE_BYTES:
                    raise ReceiptError("route evidence size is invalid")
            canonical = dict(value)
            previous = merged.setdefault(path, canonical)
            if previous != canonical:
                raise ReceiptError("route evidence changed during resolution")
    required = {task, root / "TODO.md"}
    if route_kind == "active-manager-task":
        required.add(manager)
    if not required.issubset(merged):
        raise ReceiptError("route evidence is incomplete")
    return tuple(merged[path] for path in sorted(merged, key=str))


def route_evidence_state(path: Path) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "path": str(path)}
    except OSError as exc:
        raise ReceiptError("cannot inspect route evidence") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ReceiptError("route evidence is not a safe owned regular file")
    payload = regular_file_bytes(path, maximum=MAX_ROUTE_FILE_BYTES, field="route evidence")
    try:
        _ = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("route evidence is not UTF-8") from exc
    return {
        "exists": True,
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def validate_route_snapshot(plan: Plan, *, ignore: frozenset[Path] = frozenset()) -> None:
    if datetime.now().astimezone().strftime("%Y-%m-%d") != plan.routing["route_local_date"]:
        raise ReceiptError("route date changed during submission")
    for expected in plan.route_evidence:
        path = Path(str(expected["path"]))
        if path not in ignore and route_evidence_state(path) != expected:
            raise ReceiptError("route evidence changed before acceptance")


def executed_module_sha256(module_name: str, expected_path: Path) -> str:
    module = sys.modules.get(module_name)
    if module is None:
        raise ReceiptError("helper module execution identity is unavailable")
    raw_path = getattr(module, "__file__", "")
    digest = getattr(module, "__executed_source_sha256__", "")
    if not isinstance(raw_path, str) or absolute_path(Path(raw_path)) != expected_path:
        raise ReceiptError("helper module execution path is inconsistent")
    if not isinstance(digest, str) or HASH_RE.fullmatch(digest) is None:
        raise ReceiptError("helper module execution digest is unavailable")
    return digest


def validate_helper_snapshot(plan: Plan) -> None:
    helper_bytes = regular_file_bytes(plan.helper_path, maximum=MAX_PROGRAM_BYTES, field="helper")
    receiver_bytes = regular_file_bytes(plan.receiver_path, maximum=MAX_PROGRAM_BYTES, field="receiver")
    if hashlib.sha256(helper_bytes).hexdigest() != plan.helper["sha256"]:
        raise ReceiptError("report helper source changed during the transaction")
    if hashlib.sha256(receiver_bytes).hexdigest() != plan.helper["receiver_sha256"]:
        raise ReceiptError("receipt receiver source changed during the transaction")
    dependencies = plan.helper.get("dependencies")
    if not isinstance(dependencies, dict):
        raise ReceiptError("helper dependency identity is invalid")
    for name, value in dependencies.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ReceiptError("helper dependency identity is invalid")
        raw_path = value.get("path")
        digest = value.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(digest, str):
            raise ReceiptError("helper dependency identity is invalid")
        payload = regular_file_bytes(Path(raw_path), maximum=MAX_PROGRAM_BYTES, field=f"helper dependency {name}")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ReceiptError("helper dependency source changed during the transaction")


def owner_prefix_record(binding: OwnerPrefixBinding) -> dict[str, object]:
    return {
        "manager_path_sha256": binding.manager_path_sha256,
        "separator_bytes": binding.separator_bytes,
        "sha256": binding.sha256,
        "size_bytes": binding.size_bytes,
    }


def owner_prefix_line(binding: OwnerPrefixBinding) -> str:
    return (
        f"[omo-report-owner-prefix: manager-path-sha256={binding.manager_path_sha256} sha256={binding.sha256} "
        f"size-bytes={binding.size_bytes} separator-bytes={binding.separator_bytes}]"
    )


def parse_envelope_owner_prefix(path: Path) -> OwnerPrefixBinding:
    payload = regular_file_bytes(path, maximum=MAX_ENVELOPE_BYTES, field="private envelope")
    header, separator, _message = payload.partition(b"message:\n")
    if not separator:
        raise ReceiptError("private envelope has no owner-prefix binding")
    try:
        header_lines = header.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReceiptError("private envelope owner-prefix binding is not UTF-8") from exc
    if len(header_lines) < 3:
        raise ReceiptError("private envelope has no owner-prefix binding")
    match = OWNER_PREFIX_LINE_RE.fullmatch(header_lines[2])
    if match is None:
        raise ReceiptError("private envelope owner-prefix binding is invalid")
    return OwnerPrefixBinding(match.group(1), match.group(2), int(match.group(3)), int(match.group(4)))


def manager_transaction_state(payload: bytes, binding: OwnerPrefixBinding, pointer: str) -> str:
    owner = payload[: binding.size_bytes]
    if len(owner) != binding.size_bytes or hashlib.sha256(owner).hexdigest() != binding.sha256:
        return "invalid"
    expected_separator = 1 if not owner or owner.endswith(b"\n") else 2
    if binding.separator_bytes != expected_separator:
        return "invalid"
    if len(payload) == binding.size_bytes:
        return "restored"
    suffix = b"\n" * binding.separator_bytes + b"(pending)\n" + pointer.encode("utf-8") + b"\n"
    return "active" if payload == owner + suffix else "invalid"


def bind_owner_prefix(manager: Path, envelope: Path, pointer: str) -> OwnerPrefixBinding:
    current = manager_bytes(manager)
    if validate_optional_regular(envelope, "private envelope", exact_mode=0o600):
        try:
            binding = parse_envelope_owner_prefix(envelope)
        except ReceiptError as exc:
            raise ReceiptError("stale or corrupt report file") from exc
    else:
        binding = OwnerPrefixBinding(
            hashlib.sha256(str(manager).encode()).hexdigest(),
            hashlib.sha256(current).hexdigest(),
            len(current),
            1 if not current or current.endswith(b"\n") else 2,
        )
    if binding.manager_path_sha256 != hashlib.sha256(str(manager).encode()).hexdigest():
        raise ReceiptError("private envelope owner route is inconsistent")
    if manager_transaction_state(current, binding, pointer) == "invalid":
        raise ReceiptError("manager bytes differ from the bound report transaction")
    return binding


def build_plan(args: Arguments) -> Plan:
    raw_message_path = args.message_file.expanduser()
    if not raw_message_path.is_absolute():
        raw_message_path = Path.cwd() / raw_message_path
    message_path = raw_message_path.parent.resolve(strict=False) / raw_message_path.name
    message, message_identity, message_fd = open_regular_file_snapshot(
        message_path,
        maximum=MAX_ENVELOPE_BYTES,
        field="message file",
    )
    try:
        return _build_plan_from_message(
            args,
            message_path=message_path,
            message_identity=message_identity,
            message_fd=message_fd,
            message=message,
        )
    except BaseException:
        os.close(message_fd)
        raise


def _build_plan_from_message(
    args: Arguments,
    *,
    message_path: Path,
    message_identity: tuple[int, int],
    message_fd: int,
    message: bytes,
) -> Plan:
    root = absolute_path(args.root)
    task = absolute_path(args.task)
    manager = absolute_path(args.manager)
    helper_path = absolute_path(args.helper)
    receiver_path = absolute_path(Path(__file__))
    _ = validate_directory(root, private=False, field="root")
    for candidate, field in ((task, "task"), (helper_path, "helper"), (receiver_path, "receiver")):
        if not validate_optional_regular(candidate, field):
            raise ReceiptError(f"{field} does not exist")
    try:
        task.relative_to(root)
        manager.relative_to(root)
    except ValueError as exc:
        raise ReceiptError("task and manager must be inside root") from exc
    _ = validate_directory(manager.parent, private=False, field="manager parent")
    if manager.exists() and not validate_optional_regular(manager, "manager"):
        raise ReceiptError("manager is invalid")

    status = normalize_status(args.status)
    if AGENT_RE.fullmatch(args.agent) is None or args.agent in {".", ".."}:
        raise ReceiptError("agent is invalid")
    producer_target = canonical_target(args.producer_target, required=True, field="producer target")
    requested_target = canonical_target(args.requested_manager_target, required=False, field="requested manager target")
    resolved_target = canonical_target(args.resolved_manager_target, required=False, field="resolved manager target")
    if ROUTE_KIND_RE.fullmatch(args.route_kind) is None:
        raise ReceiptError("route kind is invalid")
    validate_text_field(args.route_note, "route note", max_length=MAX_ENVELOPE_BYTES)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.route_local_date) is None:
        raise ReceiptError("route local date is invalid")
    route_evidence = parse_route_evidence(
        (args.task_route_evidence, args.manager_route_evidence),
        root=root,
        task=task,
        manager=manager,
        route_kind=args.route_kind,
    )

    try:
        message_text = message.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("message body is not UTF-8") from exc
    report_context = extract_report_context(message_text)
    input_info: dict[str, object] = {"sha256": hashlib.sha256(message).hexdigest(), "size_bytes": len(message)}
    tmux = tmux_metadata(args, producer_target)
    routing: dict[str, object] = {
        "agent": args.agent,
        "manager": str(manager),
        "producer_target": producer_target,
        "requested_manager_target": requested_target,
        "resolved_manager_target": resolved_target,
        "root": str(root),
        "route_kind": args.route_kind,
        "route_evidence": list(route_evidence),
        "route_evidence_sha256": hashlib.sha256(canonical_json(list(route_evidence)).rstrip(b"\n")).hexdigest(),
        "route_local_date": args.route_local_date,
        "route_note": args.route_note,
        "task": str(task),
        "tmux": tmux,
    }

    dependency_paths = {
        "omo_pending_digest": receiver_path.with_name("omo_pending_digest.py"),
        "omo_task_lock": receiver_path.with_name("omo_task_lock.py"),
    }
    helper_bytes = regular_file_bytes(helper_path, maximum=MAX_PROGRAM_BYTES, field="helper")
    receiver_bytes = regular_file_bytes(receiver_path, maximum=MAX_PROGRAM_BYTES, field="receiver")
    receiver_digest = hashlib.sha256(receiver_bytes).hexdigest()
    if executed_module_sha256("omo_manager.omo_report_receipt", receiver_path) != receiver_digest:
        raise ReceiptError("executed receipt receiver differs from its source identity")
    receiver_module = sys.modules.get("omo_manager.omo_report_receipt")
    executed_helper_path = getattr(receiver_module, "__executed_helper_path__", "") if receiver_module is not None else ""
    executed_helper_digest = getattr(receiver_module, "__executed_helper_sha256__", "") if receiver_module is not None else ""
    helper_digest = hashlib.sha256(helper_bytes).hexdigest()
    if executed_helper_path != str(helper_path) or executed_helper_digest != helper_digest:
        raise ReceiptError("executed report helper differs from its source identity")
    dependencies: dict[str, object] = {}
    for name, dependency_path in dependency_paths.items():
        dependency_bytes = regular_file_bytes(
            dependency_path,
            maximum=MAX_PROGRAM_BYTES,
            field=f"helper dependency {name}",
        )
        dependency_digest = hashlib.sha256(dependency_bytes).hexdigest()
        if executed_module_sha256(f"omo_manager.{name}", dependency_path) != dependency_digest:
            raise ReceiptError(f"executed helper dependency {name} differs from its source identity")
        dependencies[name] = {
            "path": str(dependency_path),
            "sha256": dependency_digest,
        }
    helper: dict[str, object] = {
        "dependencies": dependencies,
        "execution": "immutable-pipe-and-memory-compiled-sources",
        "path": str(helper_path),
        "receiver_path": str(receiver_path),
        "receiver_sha256": receiver_digest,
        "receiver_version": RECEIVER_VERSION,
        "receiver_version_sha256": hashlib.sha256(RECEIVER_VERSION.encode("utf-8")).hexdigest(),
        "sha256": helper_digest,
    }
    label = safe_label(producer_target)
    task_basename = safe_label(task.name)
    if not SAFE_VALUE_RE.fullmatch(task_basename):
        raise ReceiptError("task basename is not watcher-compatible")
    # Preserve the historical envelope name used by public callers. Protocol
    # version identity belongs in the private replay binding below.
    report_key_parts = [message, args.agent.encode(), status.encode(), label.encode(), str(task).encode()]
    if args.route_note:
        report_key_parts.append(args.route_note.encode())
    report_key = hashlib.sha256(b"\0".join(report_key_parts)).hexdigest()
    envelope_directory = Path("/tmp") / f"omo-agent-messages-{os.getuid()}"
    envelope_final = envelope_directory / f"{safe_part(args.agent)}_{safe_part(status)}_{report_key}.md"
    pointer = f"(from agent {producer_target} {envelope_final})"
    owner_prefix = bind_owner_prefix(manager, envelope_final, pointer)
    binding = {
        "helper": helper,
        "input": input_info,
        "owner_prefix": owner_prefix_record(owner_prefix),
        "report_context": report_context,
        "routing": replay_routing_identity(routing),
        "schema": BINDING_SCHEMA,
        "status": status,
    }
    replay_id = hashlib.sha256(canonical_json(binding).rstrip(b"\n")).hexdigest()
    envelope_temporary = envelope_directory / f".{envelope_final.name}.{replay_id}.tmp"
    sample_envelope = envelope_bytes(
        args.agent,
        producer_target,
        task_basename,
        "00:00",
        str(input_info["sha256"]),
        owner_prefix,
        args.route_note,
        message,
    )
    if len(sample_envelope) > MAX_ENVELOPE_BYTES:
        raise ReceiptError("authenticated report envelope exceeds the watcher size limit")

    receipt_directory = receipt_state_home() / "omo-manager" / "report-receipts"
    transaction_commitment_final = receipt_directory / f"{replay_id}.commitment"
    transaction_commitment_temporary = receipt_directory / f".{replay_id}.commitment.tmp"
    receipt_final = receipt_directory / f"{replay_id}.json"
    receipt_temporary = receipt_directory / f".{replay_id}.tmp"
    receipt_publication_final = receipt_directory / f"{replay_id}.publication.json"
    receipt_publication_temporary = receipt_directory / f".{replay_id}.publication.tmp"
    acknowledgment_state = receipt_state_home() / "omo-manager" / "pending-watch-consumed-reports.tsv"
    acknowledgment_lock = acknowledgment_state.with_name(f".{acknowledgment_state.name}.lock")
    acknowledgment_key = manager_acknowledgment_key(root, envelope_final, str(input_info["sha256"]))
    acknowledgment_temporary = watcher_report_state_temporary(acknowledgment_state, acknowledgment_key)
    acknowledgment_maintenance_temporary = watcher_report_state_maintenance_temporary(acknowledgment_state)
    acknowledgment_authority_lock = (
        acknowledgment_state.parent / "pending-watch-authority" / f"{hashlib.sha256(acknowledgment_key.encode()).hexdigest()}.lock"
    )
    acknowledgment_authority_completion = acknowledgment_authority_lock.with_name(f"{acknowledgment_authority_lock.name}.complete")
    route_lock_targets = tuple(sorted({manager, *(Path(str(item["path"])) for item in route_evidence)}, key=str))
    route_locks = tuple((target, task_file_lock_path(target)) for target in route_lock_targets)
    manager_lock = next(lock_path for target, lock_path in route_locks if target == manager)
    plan = Plan(
        mode=args.mode,
        root=root,
        task=task,
        manager=manager,
        helper_path=helper_path,
        receiver_path=receiver_path,
        message_path=message_path,
        message_identity=message_identity,
        message_fd=message_fd,
        message=message,
        status=status,
        input_info=input_info,
        report_context=report_context,
        routing=routing,
        route_evidence=route_evidence,
        route_locks=route_locks,
        helper=helper,
        replay_id=replay_id,
        owner_prefix=owner_prefix,
        report_lock=Path(f"{manager}.omo_report.lock"),
        task_lock=manager_lock,
        manager_temporary=manager.parent / f".{manager.name}.omo-report-{replay_id}.tmp",
        manager_watcher_temporary=watcher_report_manager_temporary(manager, acknowledgment_key),
        envelope_directory=envelope_directory,
        envelope_temporary=envelope_temporary,
        envelope_final=envelope_final,
        pointer=pointer,
        acknowledgment_state=acknowledgment_state,
        acknowledgment_lock=acknowledgment_lock,
        acknowledgment_temporary=acknowledgment_temporary,
        acknowledgment_maintenance_temporary=acknowledgment_maintenance_temporary,
        acknowledgment_key=acknowledgment_key,
        acknowledgment_authority_lock=acknowledgment_authority_lock,
        acknowledgment_authority_completion=acknowledgment_authority_completion,
        receipt_directory=receipt_directory,
        transaction_commitment_temporary=transaction_commitment_temporary,
        transaction_commitment_final=transaction_commitment_final,
        receipt_temporary=receipt_temporary,
        receipt_final=receipt_final,
        receipt_publication_temporary=receipt_publication_temporary,
        receipt_publication_final=receipt_publication_final,
    )
    validate_helper_snapshot(plan)
    return plan


def envelope_bytes(
    agent: str,
    producer_target: str,
    task_basename: str,
    stamp: str,
    message_hash: str,
    owner_prefix: OwnerPrefixBinding,
    route_note: str,
    message: bytes,
) -> bytes:
    lines = [
        f"(sent from {agent} via omo_report.sh tmux={producer_target} time={stamp} task-file={task_basename})",
        f"[message-sha256: {message_hash}]",
        owner_prefix_line(owner_prefix),
    ]
    if route_note:
        lines.extend(("route-warning:", route_note))
    lines.append("message:")
    return ("\n".join(lines) + "\n").encode("utf-8") + message


def directory_entry_digest(path: Path) -> str:
    try:
        names = sorted(os.listdir(os.fsencode(path)))
    except OSError as exc:
        raise ReceiptError("cannot enumerate a transaction directory") from exc
    digest = hashlib.sha256()
    for name in names:
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
    return digest.hexdigest()


def regular_sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError("cannot hash a transaction file") from exc
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptError("transaction path changed type")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ReceiptError("transaction file changed while hashing")
    finally:
        os.close(fd)
    return digest.hexdigest()


def path_state(path: Path) -> dict[str, object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    except OSError as exc:
        raise ReceiptError("cannot inspect a transaction path") from exc
    if stat.S_ISREG(info.st_mode):
        kind = "regular"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISFIFO(info.st_mode):
        kind = "fifo"
    elif stat.S_ISSOCK(info.st_mode):
        kind = "socket"
    elif stat.S_ISCHR(info.st_mode):
        kind = "character-device"
    elif stat.S_ISBLK(info.st_mode):
        kind = "block-device"
    else:
        kind = "other"
    result: dict[str, object] = {
        "ctime_ns": info.st_ctime_ns,
        "dev": info.st_dev,
        "exists": True,
        "gid": info.st_gid,
        "inode": info.st_ino,
        "mode": format(stat.S_IMODE(info.st_mode), "04o"),
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "type": kind,
        "uid": info.st_uid,
    }
    if kind == "regular":
        result["sha256"] = regular_sha256(path)
    elif kind == "directory":
        result["entry_name_sha256"] = directory_entry_digest(path)
    return result


def held_message_state(plan: Plan) -> dict[str, object]:
    try:
        info = os.fstat(plan.message_fd)
    except OSError as exc:
        raise ReceiptError("pending report draft object identity changed") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or (info.st_dev, info.st_ino) != plan.message_identity
        or info.st_size != plan.input_info["size_bytes"]
    ):
        raise ReceiptError("pending report draft object identity changed")
    return {
        "ctime_ns": info.st_ctime_ns,
        "dev": info.st_dev,
        "exists": True,
        "gid": info.st_gid,
        "inode": info.st_ino,
        "mode": format(stat.S_IMODE(info.st_mode), "04o"),
        "mtime_ns": info.st_mtime_ns,
        "sha256": plan.input_info["sha256"],
        "size": info.st_size,
        "type": "regular",
        "uid": info.st_uid,
    }


def require_absent(path: Path, field: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReceiptError(f"cannot inspect {field}") from exc
    raise ReceiptError(f"incomplete transaction residue exists for {field}")


def ensure_base_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ReceiptError("cannot create state home") from exc
    _ = validate_directory(path, private=False, field="state home")


def ensure_private_directory(path: Path, field: str) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ReceiptError(f"cannot create {field}") from exc
    _ = validate_directory(path, private=True, field=field)


def prepare_private_directories(plan: Plan) -> None:
    ensure_private_directory(plan.envelope_directory, "private envelope directory")
    state_home = plan.receipt_directory.parent.parent
    ensure_base_directory(state_home)
    ensure_private_directory(plan.receipt_directory.parent, "receipt application directory")
    ensure_private_directory(plan.receipt_directory, "receipt directory")


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ReceiptError("cannot open transaction directory for fsync") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise ReceiptError("cannot fsync transaction directory") from exc
    finally:
        os.close(fd)


def write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(fd, view[offset:])
        except OSError as exc:
            raise ReceiptError("transaction write failed") from exc
        if written <= 0:
            raise ReceiptError("transaction write was incomplete")
        offset += written


def write_new_file(path: Path, payload: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except OSError as exc:
        raise ReceiptError("cannot create transaction temporary file") from exc
    try:
        os.fchmod(fd, mode)
        write_all(fd, payload)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)


def validate_envelope(plan: Plan) -> bytes:
    payload = regular_file_bytes(plan.envelope_final, maximum=MAX_ENVELOPE_BYTES, field="private envelope")
    info = plan.envelope_final.lstat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise ReceiptError("private envelope must have mode 0600")
    header, separator, message = payload.partition(b"message:\n")
    if not separator:
        raise ReceiptError("private envelope is corrupt")
    try:
        header_lines = header.decode("utf-8").splitlines()
        _ = message.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("private envelope is not UTF-8") from exc
    if len(header_lines) not in {3, 5}:
        raise ReceiptError("private envelope header is corrupt")
    sent_match = SENT_LINE_RE.fullmatch(header_lines[0])
    hash_match = HASH_LINE_RE.fullmatch(header_lines[1])
    owner_match = OWNER_PREFIX_LINE_RE.fullmatch(header_lines[2])
    if sent_match is None or hash_match is None or owner_match is None:
        raise ReceiptError("private envelope header is invalid")
    expected_task_name = safe_label(plan.task.name)
    if sent_match.groups()[:2] != (str(plan.routing["agent"]), str(plan.routing["producer_target"])) or sent_match.group(3) != expected_task_name:
        raise ReceiptError("private envelope identity is inconsistent")
    if header_lines[2] != owner_prefix_line(plan.owner_prefix):
        raise ReceiptError("private envelope owner-prefix binding is inconsistent")
    if len(header_lines) == 5:
        if header_lines[3:] != ["route-warning:", str(plan.routing["route_note"])]:
            raise ReceiptError("private envelope route warning is inconsistent")
    elif plan.routing["route_note"]:
        raise ReceiptError("private envelope is missing its route warning")
    if message != plan.message or hash_match.group(1) != plan.input_info["sha256"]:
        raise ReceiptError("private envelope body digest is inconsistent")
    if hashlib.sha256(message).hexdigest() != hash_match.group(1):
        raise ReceiptError("private envelope body hash is corrupt")
    expected_pointer = f"(from agent {sent_match.group(2)} {plan.envelope_final})"
    if expected_pointer != plan.pointer:
        raise ReceiptError("private envelope pointer is inconsistent")
    return payload


def require_valid_envelope(plan: Plan) -> bytes:
    try:
        return validate_envelope(plan)
    except ReceiptError as exc:
        raise ReceiptError("stale or corrupt report file") from exc


def create_or_reuse_envelope(plan: Plan, stamp: str) -> str:
    require_absent(plan.envelope_temporary, "private envelope temporary file")
    if validate_optional_regular(plan.envelope_final, "private envelope", exact_mode=0o600):
        _ = require_valid_envelope(plan)
        fd = os.open(plan.envelope_final, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return "reused"
    payload = envelope_bytes(
        str(plan.routing["agent"]),
        str(plan.routing["producer_target"]),
        safe_label(plan.task.name),
        stamp,
        str(plan.input_info["sha256"]),
        plan.owner_prefix,
        str(plan.routing["route_note"]),
        plan.message,
    )
    if len(payload) > MAX_ENVELOPE_BYTES:
        raise ReceiptError("authenticated report envelope exceeds the watcher size limit")
    write_new_file(plan.envelope_temporary, payload, 0o600)
    try:
        os.link(plan.envelope_temporary, plan.envelope_final, follow_symlinks=False)
    except FileExistsError:
        try:
            plan.envelope_temporary.unlink()
        except OSError as exc:
            raise ReceiptError("cannot remove redundant private envelope temporary file") from exc
        _ = require_valid_envelope(plan)
        fsync_directory(plan.envelope_directory)
        return "reused"
    except OSError as exc:
        raise ReceiptError("cannot publish private envelope") from exc
    try:
        plan.envelope_temporary.unlink()
    except OSError as exc:
        raise ReceiptError("cannot retire private envelope temporary name") from exc
    fsync_directory(plan.envelope_directory)
    if regular_file_bytes(plan.envelope_final, maximum=MAX_ENVELOPE_BYTES, field="private envelope") != payload:
        raise ReceiptError("private envelope readback differs from committed bytes")
    _ = require_valid_envelope(plan)
    return "created"


def manager_bytes(path: Path) -> bytes:
    if not validate_optional_regular(path, "manager"):
        return b""
    payload = regular_file_bytes(path, maximum=64 * 1024 * 1024, field="manager")
    try:
        _ = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReceiptError("manager file is not UTF-8") from exc
    return payload


def pointer_state(manager_payload: bytes, plan: Plan) -> str:
    try:
        lines = manager_payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReceiptError("manager file is not UTF-8") from exc
    exact_indices = [index for index, line in enumerate(lines) if line == plan.pointer]
    envelope_text = str(plan.envelope_final)
    other_references = [line for line in lines if envelope_text in line and line != plan.pointer]
    if other_references:
        raise ReceiptError("manager file contains an inconsistent report pointer")
    hash_line = f"[message-sha256: {plan.input_info['sha256']}]"
    legacy_line = f"(from agent {plan.routing['agent']} via omo_report.sh status={plan.status})"
    source_prefix = f"[omo-message-source: origin=agent agent={plan.routing['agent']} via=omo_report.sh status={plan.status}"
    states: list[str] = []
    for index in exact_indices:
        if index == 0 or lines[index - 1] != "(pending)":
            raise ReceiptError("manager report pointer is not adjacent to a pending marker")
        end = next((next_index for next_index in range(index + 1, len(lines)) if lines[next_index].strip() == "(pending)"), len(lines))
        handled = any(lines[next_index].strip().startswith(("(manager handled:", "(manager routed:")) for next_index in range(index + 1, end))
        states.append("handled" if handled else "active")
    for pending_index, line in enumerate(lines):
        if line.strip() != "(pending)":
            continue
        end = next((index for index in range(pending_index + 1, len(lines)) if lines[index].strip() == "(pending)"), len(lines))
        block = [item.strip() for item in lines[pending_index:end]]
        if hash_line not in block:
            continue
        metadata = [item for item in block if item.startswith(source_prefix)]
        source_matches = legacy_line in block and not metadata
        for item in metadata:
            target_match = re.search(r" tmux_target=([^ \]]+)", item)
            if target_match is not None and canonical_target(target_match.group(1), required=True, field="legacy tmux target") == plan.routing["producer_target"]:
                source_matches = True
        if source_matches:
            handled = any(item.startswith(("(manager handled:", "(manager routed:")) for item in block[1:])
            states.append("legacy-handled" if handled else "legacy-active")
    active_states = [state for state in states if state in {"active", "legacy-active"}]
    if len(active_states) > 1:
        raise ReceiptError("manager file contains ambiguous matching report markers")
    if active_states:
        return active_states[0]
    return "handled" if states else "absent"


def append_payload(current: bytes, plan: Plan) -> bytes:
    if manager_transaction_state(current, plan.owner_prefix, plan.pointer) != "restored":
        raise ReceiptError("manager owner bytes are not at the bound pre-append state")
    separator = b"\n" * plan.owner_prefix.separator_bytes
    return current + separator + b"(pending)\n" + plan.pointer.encode("utf-8") + b"\n"


def appended_manager_size(plan: Plan) -> int:
    return plan.owner_prefix.size_bytes + plan.owner_prefix.separator_bytes + len(b"(pending)\n") + len(plan.pointer.encode("utf-8")) + 1


def replace_manager(plan: Plan, current: bytes, replacement: bytes) -> None:
    require_absent(plan.manager_temporary, "manager temporary file")
    if validate_optional_regular(plan.manager, "manager"):
        before = plan.manager.lstat()
        mode = stat.S_IMODE(before.st_mode)
    else:
        before = None
        mode = 0o600
    write_new_file(plan.manager_temporary, replacement, mode)
    try:
        current_info = plan.manager.lstat()
    except FileNotFoundError:
        current_info = None
    except OSError as exc:
        raise ReceiptError("cannot guard manager replacement") from exc
    if before is None:
        if current_info is not None:
            raise ReceiptError("manager file appeared during transaction")
    elif current_info is None or (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        current_info.st_dev,
        current_info.st_ino,
        current_info.st_size,
        current_info.st_mtime_ns,
        current_info.st_ctime_ns,
    ):
        raise ReceiptError("manager file changed during transaction")
    try:
        os.replace(plan.manager_temporary, plan.manager)
    except OSError as exc:
        raise ReceiptError("cannot publish manager update") from exc
    fsync_directory(plan.manager.parent)
    if manager_bytes(plan.manager) != replacement:
        raise ReceiptError("manager file readback differs from committed bytes")
    if pointer_state(replacement, plan) != "active":
        raise ReceiptError("manager report pointer verification failed")


def append_or_deduplicate_manager(plan: Plan) -> str:
    current = manager_bytes(plan.manager)
    existed = plan.manager.exists()
    state = pointer_state(current, plan)
    if state == "legacy-active":
        raise ReceiptError("legacy report marker cannot establish receipt acceptance")
    if state == "active":
        if plan.manager.exists():
            fd = os.open(plan.manager, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return "existing-pointer-fsynced"
    replacement = append_payload(current, plan)
    replace_manager(plan, current, replacement)
    return "atomic-replace-appended-pointer" if existed else "atomic-create-appended-pointer"


def validate_lock_file(path: Path, field: str) -> None:
    if not validate_optional_regular(path, field):
        raise ReceiptError(f"{field} was not created")


@contextmanager
def adjacent_report_lock(path: Path) -> Iterator[None]:
    if path.exists():
        _ = validate_optional_regular(path, "adjacent report lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ReceiptError("cannot open adjacent report lock") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ReceiptError("adjacent report lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def validate_private_layout(plan: Plan, *, allow_publication_recovery: bool = False) -> None:
    if validate_optional_directory(plan.envelope_directory, private=True, field="private envelope directory"):
        require_absent(plan.envelope_temporary, "private envelope temporary file")
    elif plan.envelope_final.exists() or plan.envelope_temporary.exists():
        raise ReceiptError("private envelope directory is missing")
    state_home = plan.receipt_directory.parent.parent
    if state_home.exists():
        _ = validate_directory(state_home, private=False, field="state home")
    if plan.receipt_directory.parent.exists():
        _ = validate_directory(plan.receipt_directory.parent, private=True, field="receipt application directory")
    if plan.receipt_directory.exists():
        _ = validate_directory(plan.receipt_directory, private=True, field="receipt directory")
    elif (
        plan.transaction_commitment_final.exists()
        or plan.transaction_commitment_temporary.exists()
        or plan.receipt_final.exists()
        or plan.receipt_temporary.exists()
    ):
        raise ReceiptError("receipt directory is missing")
    require_absent(plan.transaction_commitment_temporary, "transaction commitment temporary file")
    if plan.transaction_commitment_final.exists():
        _ = validate_optional_regular(
            plan.transaction_commitment_final,
            "transaction commitment",
            exact_mode=0o600,
        )
    require_absent(plan.receipt_temporary, "receipt temporary file")
    if not (allow_publication_recovery and plan.receipt_final.exists() and not plan.receipt_publication_final.exists()):
        require_absent(plan.receipt_publication_temporary, "receipt publication temporary file")
    require_absent(plan.manager_temporary, "manager temporary file")
    require_absent(plan.manager_watcher_temporary, "watcher manager temporary file")
    require_absent(plan.acknowledgment_temporary, "watcher acknowledgment temporary file")
    require_absent(plan.acknowledgment_maintenance_temporary, "watcher acknowledgment maintenance temporary file")
    if plan.acknowledgment_authority_lock.parent.exists():
        _ = validate_directory(
            plan.acknowledgment_authority_lock.parent,
            private=True,
            field="manager acknowledgment authority directory",
        )
    if plan.acknowledgment_authority_lock.exists():
        _ = validate_optional_regular(
            plan.acknowledgment_authority_lock,
            "manager acknowledgment authority lock",
            exact_mode=0o600,
        )
    if plan.acknowledgment_authority_completion.exists():
        _ = validate_optional_regular(
            plan.acknowledgment_authority_completion,
            "manager acknowledgment authority completion",
            exact_mode=0o600,
        )
    if plan.task_lock.parent.exists():
        _ = validate_directory(plan.task_lock.parent, private=True, field="task file lock directory")
    for _, lock_path in plan.route_locks:
        if lock_path.exists():
            _ = validate_optional_regular(lock_path, "route transaction lock")
    if plan.report_lock.exists():
        _ = validate_optional_regular(plan.report_lock, "adjacent report lock")


def receipt_record(plan: Plan) -> dict[str, object]:
    return {
        "application_directory": str(plan.receipt_directory.parent),
        "commit": "write-fsync-rename-fsync-directory",
        "directory": str(plan.receipt_directory),
        "directory_mode": "0700",
        "file_mode": "0600",
        "final": str(plan.receipt_final),
        "publication_final": str(plan.receipt_publication_final),
        "publication_temporary": str(plan.receipt_publication_temporary),
        "state_home": str(plan.receipt_directory.parent.parent),
        "temporary": str(plan.receipt_temporary),
    }


def forbidden_receipt_content(value: object) -> bool:
    forbidden = {"body", "message", "message-file", "message_file", "message_path", "draft", "draft_path"}
    if isinstance(value, dict):
        return any(str(key) in forbidden or forbidden_receipt_content(item) for key, item in value.items())
    if isinstance(value, list):
        return any(forbidden_receipt_content(item) for item in value)
    return False


def validate_receipt_bytes(
    plan: Plan,
    payload: bytes,
    *,
    allow_publication_recovery: bool = False,
) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("durable receipt is not valid JSON") from exc
    if not isinstance(parsed, dict) or canonical_json(parsed) != payload:
        raise ReceiptError("durable receipt is not canonical JSON")
    expected_keys = {
        "accepted",
        "accepted_at_utc",
        "helper",
        "input",
        "preflight",
        "receipt_id",
        "receipt_record",
        "replay_id",
        "report_context",
        "routing",
        "schema",
        "side_effects",
        "status",
    }
    if set(parsed) != expected_keys or forbidden_receipt_content(parsed):
        raise ReceiptError("durable receipt schema is invalid")
    if parsed.get("schema") != RECEIPT_SCHEMA or parsed.get("accepted") is not True or parsed.get("replay_id") != plan.replay_id:
        raise ReceiptError("durable receipt identity is invalid")
    receipt_without_id = dict(parsed)
    receipt_id = receipt_without_id.pop("receipt_id", None)
    if not isinstance(receipt_id, str) or receipt_id != bound_receipt_id(receipt_without_id):
        raise ReceiptError("durable receipt content binding is invalid")
    if parsed.get("status") != plan.status or parsed.get("input") != plan.input_info or parsed.get("report_context") != plan.report_context:
        raise ReceiptError("durable receipt input binding is inconsistent")
    preflight = parsed.get("preflight")
    preflight_allocation = preflight.get("allocation") if isinstance(preflight, dict) else None
    realized_allocation_file = preflight_allocation.get("file") if isinstance(preflight_allocation, dict) else None
    if not isinstance(preflight, dict) or not isinstance(realized_allocation_file, str) or not Path(realized_allocation_file).is_absolute():
        raise ReceiptError("durable receipt preflight transaction binding is inconsistent")
    transaction_commitment = read_transaction_commitment(
        plan,
        expected_preflight=preflight,
        require_current_allocation_identity=False,
    )
    if transaction_commitment is None:
        raise ReceiptError("durable receipt has no immutable transaction commitment")
    routing_sources = preflight.get("routing_sources")
    if not isinstance(routing_sources, list):
        raise ReceiptError("durable receipt preflight route evidence is invalid")
    routing = parsed.get("routing")
    if not isinstance(routing, dict) or set(routing) != set(plan.routing):
        raise ReceiptError("durable receipt routing is invalid")
    for key, value in replay_routing_identity(plan.routing).items():
        if routing.get(key) != value:
            raise ReceiptError("durable receipt routing is inconsistent")
    if not isinstance(routing.get("tmux"), dict):
        raise ReceiptError("durable receipt tmux metadata is invalid")
    if (
        routing.get("route_evidence") != routing_sources
        or routing.get("route_evidence_sha256")
        != hashlib.sha256(canonical_json(routing_sources).rstrip(b"\n")).hexdigest()
    ):
        raise ReceiptError("durable receipt route evidence is inconsistent")
    if parsed.get("helper") != plan.helper:
        raise ReceiptError("durable receipt helper identity is inconsistent")
    if parsed.get("receipt_record") != receipt_record(plan):
        raise ReceiptError("durable receipt path is inconsistent")
    accepted_at = parsed.get("accepted_at_utc")
    if not isinstance(accepted_at, str) or not accepted_at.endswith("Z"):
        raise ReceiptError("durable receipt acceptance time is invalid")
    try:
        accepted_time = datetime.fromisoformat(accepted_at[:-1] + "+00:00")
    except ValueError as exc:
        raise ReceiptError("durable receipt acceptance time is invalid") from exc
    if accepted_time.utcoffset() != timezone.utc.utcoffset(accepted_time):
        raise ReceiptError("durable receipt acceptance time is not UTC")
    effects = parsed.get("side_effects")
    expected_effects = {
        "durable_receipt",
        "locks",
        "manager_acknowledgment",
        "manager_file",
        "private_allocation",
        "private_envelope",
        "receipt_publication",
    }
    if not isinstance(effects, dict) or set(effects) != expected_effects:
        raise ReceiptError("durable receipt side effects are invalid")
    locks = effects.get("locks")
    acknowledgment_effect = effects.get("manager_acknowledgment")
    allocation_effect = effects.get("private_allocation")
    manager_effect = effects.get("manager_file")
    envelope_effect = effects.get("private_envelope")
    receipt_effect = effects.get("durable_receipt")
    publication_effect = effects.get("receipt_publication")
    if (
        not isinstance(locks, dict)
        or set(locks) != {"adjacent_report", "route_evidence", "task_file"}
        or not isinstance(acknowledgment_effect, dict)
        or not isinstance(allocation_effect, dict)
        or not isinstance(manager_effect, dict)
        or not isinstance(envelope_effect, dict)
        or not isinstance(receipt_effect, dict)
        or not isinstance(publication_effect, dict)
    ):
        raise ReceiptError("durable receipt side effects are malformed")
    committed_allocation = transaction_commitment.get("allocation")
    committed_effect = transaction_commitment.get("commitment")
    expected_allocation_effect = {
        **committed_allocation,
        "transaction_commitment": {
            **committed_effect,
            "after": path_state(plan.transaction_commitment_final),
            "commitment_id": transaction_commitment["commitment_id"],
            "temporary_after": path_state(plan.transaction_commitment_temporary),
        },
    } if isinstance(committed_allocation, dict) and isinstance(committed_effect, dict) else None
    if allocation_effect != expected_allocation_effect:
        raise ReceiptError("durable receipt transaction commitment effect is inconsistent")
    if (
        acknowledgment_effect.get("key") != plan.acknowledgment_key
        or acknowledgment_effect.get("state") != str(plan.acknowledgment_state)
        or acknowledgment_effect.get("schema") != "omo-pending-watch-consumed-report/v1"
    ):
        raise ReceiptError("durable receipt manager acknowledgment is inconsistent")
    recorded_at = acknowledgment_effect.get("recorded_at_unix_s")
    if not isinstance(recorded_at, (int, float)) or not math.isfinite(recorded_at) or recorded_at <= 0:
        raise ReceiptError("durable receipt manager acknowledgment time is invalid")
    transition = acknowledgment_effect.get("transition")
    transition_fields = {
        "after_sha256",
        "after_size_bytes",
        "before_sha256",
        "before_size_bytes",
        "manager_path_sha256",
        "pointer_sha256",
        "protocol",
    }
    if not isinstance(transition, dict) or set(transition) not in {frozenset(transition_fields), frozenset({*transition_fields, "authority"})}:
        raise ReceiptError("durable receipt manager transition is malformed")
    expected_manager_digest = hashlib.sha256(str(plan.manager).encode()).hexdigest()
    expected_pointer_digest = hashlib.sha256(plan.pointer.encode()).hexdigest()
    before_digest = transition.get("before_sha256")
    after_digest = transition.get("after_sha256")
    before_size = transition.get("before_size_bytes")
    after_size = transition.get("after_size_bytes")
    if (
        transition.get("protocol") != "watcher-locked-pointer-transition-v1"
        or transition.get("manager_path_sha256") != expected_manager_digest
        or transition.get("pointer_sha256") != expected_pointer_digest
        or not isinstance(before_digest, str)
        or HASH_RE.fullmatch(before_digest) is None
        or not isinstance(after_digest, str)
        or HASH_RE.fullmatch(after_digest) is None
        or before_digest == after_digest
        or not isinstance(before_size, int)
        or isinstance(before_size, bool)
        or not isinstance(after_size, int)
        or isinstance(after_size, bool)
        or before_size != appended_manager_size(plan)
        or after_size != plan.owner_prefix.size_bytes
        or after_digest != plan.owner_prefix.sha256
    ):
        raise ReceiptError("durable receipt manager transition is inconsistent")
    entry_fields = [
        f"{recorded_at:.6f}",
        plan.acknowledgment_key,
        str(transition["protocol"]),
        expected_manager_digest,
        expected_pointer_digest,
        before_digest,
        str(before_size),
        after_digest,
        str(after_size),
    ]
    authority = transition.get("authority")
    if authority is not None:
        expected_authority_fields = {
            "lock_dev",
            "lock_inode",
            "lock_path_sha256",
            "pid",
            "process_start_ticks",
            "protocol",
            "role",
            "source_path",
            "source_sha256",
            "token_sha256",
        }
        if not isinstance(authority, dict) or set(authority) != expected_authority_fields:
            raise ReceiptError("durable receipt manager authority is malformed")
        role = authority.get("role")
        pid = authority.get("pid")
        start_ticks = authority.get("process_start_ticks")
        lock_dev = authority.get("lock_dev")
        lock_inode = authority.get("lock_inode")
        source_path = authority.get("source_path")
        source_digest = authority.get("source_sha256")
        token_digest = authority.get("token_sha256")
        if (
            authority.get("protocol") != "watcher-consumption-authority-v1"
            or role not in {"watcher-process", "bounded-watcher-lease"}
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(start_ticks, int)
            or isinstance(start_ticks, bool)
            or start_ticks <= 0
            or authority.get("lock_path_sha256") != hashlib.sha256(str(plan.acknowledgment_authority_lock).encode()).hexdigest()
            or not isinstance(lock_dev, int)
            or isinstance(lock_dev, bool)
            or lock_dev < 0
            or not isinstance(lock_inode, int)
            or isinstance(lock_inode, bool)
            or lock_inode <= 0
            or not isinstance(source_path, str)
            or not Path(source_path).is_absolute()
            or Path(source_path).name != ("omo_pending_watch.py" if role == "watcher-process" else "omo_task_lock.py")
            or not isinstance(source_digest, str)
            or HASH_RE.fullmatch(source_digest) is None
            or not isinstance(token_digest, str)
            or HASH_RE.fullmatch(token_digest) is None
            or (role == "watcher-process" and token_digest != "0" * 64)
        ):
            raise ReceiptError("durable receipt manager authority is inconsistent")
        dependencies = plan.helper.get("dependencies")
        task_lock_dependency = dependencies.get("omo_task_lock") if isinstance(dependencies, dict) else None
        if not isinstance(task_lock_dependency, dict) or task_lock_dependency.get("sha256") != source_digest:
            raise ReceiptError("durable receipt manager authority source is unbound")
        entry_fields.extend(
            (
                "watcher-consumption-authority-v1",
                role,
                str(pid),
                str(start_ticks),
                str(authority["lock_path_sha256"]),
                str(lock_dev),
                str(lock_inode),
                source_path,
                source_digest,
                token_digest,
            )
        )
    entry = "\t".join(entry_fields)
    expected_ack_digest = hashlib.sha256(entry.encode()).hexdigest()
    if acknowledgment_effect.get("entry_sha256") != expected_ack_digest or accepted_time.timestamp() < recorded_at:
        raise ReceiptError("durable receipt was not accepted after manager acknowledgment")
    allocation_directory = Path("/tmp") / f"omo-report-drafts-{os.getuid()}"
    allocation_file = allocation_effect.get("file")
    allocation_file_state = allocation_effect.get("file_at_submission")
    allocation_directory_state = allocation_effect.get("directory_at_submission")
    helper_allocated = isinstance(allocation_file, str) and Path(allocation_file).parent == allocation_directory
    if (
        not isinstance(allocation_file, str)
        or not Path(allocation_file).is_absolute()
        or allocation_file != realized_allocation_file
        or allocation_effect.get("directory") != str(allocation_directory)
        or allocation_effect.get("operation") != "completed-before-submit"
        or allocation_effect.get("protocol") != "exclusive-create-0600-draft-in-0700-directory"
        or not isinstance(allocation_file_state, dict)
        or allocation_file_state.get("exists") is not True
        or allocation_file_state.get("type") != "regular"
        or allocation_file_state.get("uid") != os.getuid()
        or allocation_file_state.get("sha256") != plan.input_info["sha256"]
        or allocation_file_state.get("size") != plan.input_info["size_bytes"]
        or not isinstance(allocation_directory_state, dict)
        or allocation_effect.get("helper_allocated") != helper_allocated
    ):
        raise ReceiptError("durable receipt allocation evidence is inconsistent")
    if helper_allocated and (
        allocation_directory_state.get("exists") is not True
        or allocation_directory_state.get("type") != "directory"
        or allocation_directory_state.get("uid") != os.getuid()
        or allocation_directory_state.get("mode") != "0700"
    ):
        raise ReceiptError("durable receipt helper allocation directory is inconsistent")
    if (
        manager_effect.get("path") != str(plan.manager)
        or manager_effect.get("record_pointer") != plan.pointer
        or manager_effect.get("owner_prefix") != owner_prefix_record(plan.owner_prefix)
        or manager_effect.get("temporary") != str(plan.manager_temporary)
        or manager_effect.get("temporary_after") != {"exists": False}
        or manager_effect.get("watcher_temporary") != str(plan.manager_watcher_temporary)
        or manager_effect.get("watcher_temporary_after") != {"exists": False}
    ):
        raise ReceiptError("durable receipt manager effect is inconsistent")
    manager_operation = manager_effect.get("operation")
    if manager_operation not in {
        "atomic-create-appended-pointer",
        "atomic-replace-appended-pointer",
        "existing-pointer-fsynced",
        "external-manager-acknowledged-no-active-pointer",
    }:
        raise ReceiptError("durable receipt manager operation is invalid")
    if manager_operation == "external-manager-acknowledged-no-active-pointer" and (
        manager_effect.get("before") != manager_effect.get("after") or manager_effect.get("temporary_before") != {"exists": False} or manager_effect.get("temporary_after") != {"exists": False}
    ):
        raise ReceiptError("durable receipt external manager operation is inconsistent")
    manager_after = manager_effect.get("after")
    if (
        not isinstance(manager_after, dict)
        or manager_after.get("exists") is not True
        or manager_after.get("type") != "regular"
        or manager_after.get("sha256") != plan.owner_prefix.sha256
        or manager_after.get("size") != plan.owner_prefix.size_bytes
    ):
        raise ReceiptError("durable receipt does not prove exact owner-byte restoration")
    if (
        envelope_effect.get("final") != str(plan.envelope_final)
        or envelope_effect.get("temporary") != str(plan.envelope_temporary)
        or envelope_effect.get("temporary_after") != {"exists": False}
    ):
        raise ReceiptError("durable receipt envelope effect is inconsistent")
    if (
        receipt_effect.get("final") != str(plan.receipt_final)
        or receipt_effect.get("temporary") != str(plan.receipt_temporary)
        or receipt_effect.get("temporary_after") != {"exists": False}
    ):
        raise ReceiptError("durable receipt record effect is inconsistent")
    if (
        publication_effect.get("final") != str(plan.receipt_publication_final)
        or publication_effect.get("temporary") != str(plan.receipt_publication_temporary)
        or publication_effect.get("temporary_after") != {"exists": False}
    ):
        raise ReceiptError("durable receipt publication effect is inconsistent")
    if (
        acknowledgment_effect.get("temporary") != str(plan.acknowledgment_temporary)
        or acknowledgment_effect.get("temporary_after") != {"exists": False}
        or acknowledgment_effect.get("maintenance_temporary") != str(plan.acknowledgment_maintenance_temporary)
        or acknowledgment_effect.get("maintenance_temporary_after") != {"exists": False}
        or acknowledgment_effect.get("authority_directory") != str(plan.acknowledgment_authority_lock.parent)
        or acknowledgment_effect.get("authority_lock") != str(plan.acknowledgment_authority_lock)
        or acknowledgment_effect.get("authority_completion") != str(plan.acknowledgment_authority_completion)
    ):
        raise ReceiptError("durable receipt watcher path effects are inconsistent")
    report_lock = locks.get("adjacent_report")
    task_lock = locks.get("task_file")
    route_locks = locks.get("route_evidence")
    if not isinstance(report_lock, dict) or not isinstance(task_lock, dict) or not isinstance(route_locks, list):
        raise ReceiptError("durable receipt lock effects are malformed")
    if report_lock.get("path") != str(plan.report_lock) or task_lock.get("path") != str(plan.task_lock):
        raise ReceiptError("durable receipt lock paths are inconsistent")
    expected_route_locks = {(str(target), str(lock_path)) for target, lock_path in plan.route_locks if target != plan.manager}
    actual_route_locks = {(str(item.get("source")), str(item.get("path"))) for item in route_locks if isinstance(item, dict)}
    if len(actual_route_locks) != len(route_locks) or actual_route_locks != expected_route_locks:
        raise ReceiptError("durable receipt route lock paths are inconsistent")
    validate_private_layout(plan, allow_publication_recovery=allow_publication_recovery)
    return parsed


def read_existing_receipt(plan: Plan) -> bytes | None:
    if not validate_optional_regular(plan.receipt_final, "durable receipt", exact_mode=0o600):
        return None
    payload = regular_file_bytes(plan.receipt_final, maximum=MAX_RECEIPT_BYTES, field="durable receipt")
    _ = validate_receipt_bytes(plan, payload, allow_publication_recovery=True)
    return payload


def manager_acknowledgment_timeout_s() -> float:
    raw = os.environ.get("OMO_REPORT_ACK_TIMEOUT_S", str(DEFAULT_ACK_TIMEOUT_S))
    try:
        timeout_s = float(raw)
    except ValueError as exc:
        raise ReceiptError("OMO_REPORT_ACK_TIMEOUT_S is invalid") from exc
    if not math.isfinite(timeout_s) or timeout_s < 0 or timeout_s > 60:
        raise ReceiptError("OMO_REPORT_ACK_TIMEOUT_S must be between 0 and 60 seconds")
    return timeout_s


def verify_manager_acknowledgment_authority(
    plan: Plan,
    *,
    role: str,
    pid: int,
    start_ticks: int,
    lock_digest: str,
    lock_dev: int,
    lock_inode: int,
    source_path: Path,
    source_digest: str,
    token_digest: str,
) -> dict[str, object] | None:
    if (
        role != "bounded-watcher-lease"
        or pid <= 1
        or start_ticks <= 0
        or lock_digest != hashlib.sha256(str(plan.acknowledgment_authority_lock).encode()).hexdigest()
        or lock_dev < 0
        or lock_inode <= 0
        or not source_path.is_absolute()
        or HASH_RE.fullmatch(source_digest) is None
        or HASH_RE.fullmatch(token_digest) is None
    ):
        raise ReceiptError("manager acknowledgment authority is malformed")
    if source_path.name != "omo_task_lock.py":
        raise ReceiptError("manager acknowledgment authority source is inconsistent")
    dependencies = plan.helper.get("dependencies")
    task_lock_dependency = dependencies.get("omo_task_lock") if isinstance(dependencies, dict) else None
    if not isinstance(task_lock_dependency, dict) or task_lock_dependency.get("sha256") != source_digest:
        return None
    if not watcher_report_authority_is_live(
        pid=pid,
        start_ticks=start_ticks,
        lock_path=plan.acknowledgment_authority_lock,
        lock_dev=lock_dev,
        lock_inode=lock_inode,
        source_path=source_path,
        source_sha256=source_digest,
        token_sha256=token_digest,
    ):
        return None
    return {
        "lock_dev": lock_dev,
        "lock_inode": lock_inode,
        "lock_path_sha256": lock_digest,
        "pid": pid,
        "process_start_ticks": start_ticks,
        "protocol": "watcher-consumption-authority-v1",
        "role": role,
        "source_path": str(source_path),
        "source_sha256": source_digest,
        "token_sha256": token_digest,
    }


def read_manager_acknowledgment(plan: Plan) -> dict[str, object] | None:
    if not validate_optional_regular(
        plan.acknowledgment_state,
        "manager acknowledgment state",
        exact_mode=0o600,
    ):
        return None
    payload = regular_file_bytes(
        plan.acknowledgment_state,
        maximum=MAX_ACK_STATE_BYTES,
        field="manager acknowledgment state",
    )
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReceiptError("manager acknowledgment state is not UTF-8") from exc
    matches: list[tuple[float, str, dict[str, object]]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 2 or fields[1] != plan.acknowledgment_key:
            continue
        if len(fields) in {2, 9}:
            continue
        if len(fields) != 19:
            raise ReceiptError("manager acknowledgment transition is malformed")
        (
            timestamp_text,
            key,
            protocol,
            manager_digest,
            pointer_digest,
            before_digest,
            before_size_text,
            after_digest,
            after_size_text,
            authority_protocol,
            authority_role,
            authority_pid_text,
            authority_start_text,
            authority_lock_digest,
            authority_lock_dev_text,
            authority_lock_inode_text,
            authority_source_path,
            authority_source_digest,
            authority_token_digest,
        ) = fields
        try:
            timestamp_s = float(timestamp_text)
            before_size = int(before_size_text)
            after_size = int(after_size_text)
            authority_pid = int(authority_pid_text)
            authority_start = int(authority_start_text)
            authority_lock_dev = int(authority_lock_dev_text)
            authority_lock_inode = int(authority_lock_inode_text)
        except ValueError as exc:
            raise ReceiptError("manager acknowledgment transition is invalid") from exc
        if not math.isfinite(timestamp_s) or timestamp_s <= 0:
            raise ReceiptError("manager acknowledgment time is invalid")
        if (
            protocol != "watcher-locked-pointer-transition-v1"
            or manager_digest != hashlib.sha256(str(plan.manager).encode()).hexdigest()
            or pointer_digest != hashlib.sha256(plan.pointer.encode()).hexdigest()
            or HASH_RE.fullmatch(before_digest) is None
            or HASH_RE.fullmatch(after_digest) is None
            or before_digest == after_digest
            or before_size != appended_manager_size(plan)
            or after_size != plan.owner_prefix.size_bytes
            or after_digest != plan.owner_prefix.sha256
        ):
            raise ReceiptError("manager acknowledgment transition is inconsistent")
        if authority_protocol != "watcher-consumption-authority-v1":
            raise ReceiptError("manager acknowledgment authority protocol is inconsistent")
        authority = verify_manager_acknowledgment_authority(
            plan,
            role=authority_role,
            pid=authority_pid,
            start_ticks=authority_start,
            lock_digest=authority_lock_digest,
            lock_dev=authority_lock_dev,
            lock_inode=authority_lock_inode,
            source_path=Path(authority_source_path),
            source_digest=authority_source_digest,
            token_digest=authority_token_digest,
        )
        if authority is None:
            continue
        transition: dict[str, object] = {
            "authority": authority,
            "after_sha256": after_digest,
            "after_size_bytes": after_size,
            "before_sha256": before_digest,
            "before_size_bytes": before_size,
            "manager_path_sha256": manager_digest,
            "pointer_sha256": pointer_digest,
            "protocol": protocol,
        }
        matches.append((timestamp_s, line, transition))
    if not matches:
        return None
    timestamp_s, entry, transition = max(matches, key=lambda item: item[0])
    return {
        "entry_sha256": hashlib.sha256(entry.encode()).hexdigest(),
        "key": plan.acknowledgment_key,
        "recorded_at_unix_s": timestamp_s,
        "schema": "omo-pending-watch-consumed-report/v1",
        "state": str(plan.acknowledgment_state),
        "transition": transition,
    }


def wait_for_manager_acknowledgment(plan: Plan) -> dict[str, object] | None:
    timeout_s = manager_acknowledgment_timeout_s()
    deadline = time.monotonic() + timeout_s
    while True:
        acknowledgment = read_manager_acknowledgment(plan)
        if acknowledgment is not None:
            return acknowledgment
        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return None
        time.sleep(min(0.05, remaining_s))


def acknowledgment_publication_is_complete(plan: Plan, acknowledgment: dict[str, object]) -> bool:
    if not validate_optional_regular(
        plan.acknowledgment_authority_completion,
        "manager acknowledgment authority completion",
        exact_mode=0o600,
    ):
        return False
    entry_digest = acknowledgment.get("entry_sha256")
    if not isinstance(entry_digest, str) or HASH_RE.fullmatch(entry_digest) is None:
        raise ReceiptError("manager acknowledgment publication binding is malformed")
    payload = regular_file_bytes(
        plan.acknowledgment_authority_completion,
        maximum=256,
        field="manager acknowledgment authority completion",
    )
    return payload == f"{entry_digest}\n".encode()


def validate_consistency(
    plan: Plan,
    receipt_payload: bytes | None,
    acknowledgment: dict[str, object] | None,
) -> tuple[bool, str]:
    envelope_exists = validate_optional_regular(plan.envelope_final, "private envelope", exact_mode=0o600)
    manager_payload = manager_bytes(plan.manager)
    pointer = pointer_state(manager_payload, plan)
    if pointer == "legacy-active":
        raise ReceiptError("legacy report marker cannot establish receipt acceptance")
    owner_state = manager_transaction_state(manager_payload, plan.owner_prefix, plan.pointer)
    expected_pointer = "active" if owner_state == "active" else "absent"
    if owner_state == "invalid" or pointer != expected_pointer:
        raise ReceiptError("manager pointer and bound owner bytes are inconsistent")
    if envelope_exists:
        _ = require_valid_envelope(plan)
    if receipt_payload is None:
        if acknowledgment is not None and not envelope_exists:
            raise ReceiptError("manager acknowledgment has no valid private envelope")
        if pointer != "absent" and not envelope_exists:
            raise ReceiptError("report pointer has no valid private envelope")
    else:
        if not envelope_exists:
            raise ReceiptError("durable receipt has no valid private envelope")
        if not plan.manager.exists():
            raise ReceiptError("durable receipt and manager pointer state are inconsistent")
    return receipt_payload is not None, pointer


def public_routing(plan: Plan) -> dict[str, object]:
    keys = (
        "manager",
        "producer_target",
        "requested_manager_target",
        "resolved_manager_target",
        "route_kind",
        "task",
    )
    return {key: plan.routing[key] for key in keys}


def preflight_transaction_set(
    plan: Plan,
    *,
    allocation_file: Path | None = None,
    routing_sources: tuple[dict[str, object], ...] | None = None,
) -> dict[str, object]:
    realized_allocation_file = plan.message_path if allocation_file is None else allocation_file
    realized_routing_sources = plan.route_evidence if routing_sources is None else routing_sources
    directories = sorted(
        {
            str(Path("/tmp") / f"omo-report-drafts-{os.getuid()}"),
            str(plan.envelope_directory),
            str(plan.manager.parent),
            str(plan.task_lock.parent),
            str(plan.receipt_directory.parent.parent),
            str(plan.receipt_directory.parent),
            str(plan.acknowledgment_authority_lock.parent),
            str(plan.receipt_directory),
        }
    )
    locks = {
        "acknowledgment": str(plan.acknowledgment_lock),
        "adjacent_report": str(plan.report_lock),
        "routing": [
            {"path": str(lock_path), "source": str(source)}
            for source, lock_path in plan.route_locks
        ],
        "watcher_authority": str(plan.acknowledgment_authority_lock),
    }
    records = {
        "acknowledgment_ledger": str(plan.acknowledgment_state),
        "authority_completion": str(plan.acknowledgment_authority_completion),
        "manager": str(plan.manager),
        "private_envelope": str(plan.envelope_final),
        "private_receipt": str(plan.receipt_final),
        "producer": str(plan.task),
        "receipt_publication": str(plan.receipt_publication_final),
        "transaction_commitment": str(plan.transaction_commitment_final),
    }
    temporary_files = sorted(
        {
            str(plan.envelope_temporary),
            str(plan.manager_temporary),
            str(plan.manager_watcher_temporary),
            str(plan.acknowledgment_temporary),
            str(plan.acknowledgment_maintenance_temporary),
            str(plan.receipt_temporary),
            str(plan.receipt_publication_temporary),
            str(plan.transaction_commitment_temporary),
        }
    )
    transaction: dict[str, object] = {
        "allocation": {
            "directory": str(Path("/tmp") / f"omo-report-drafts-{os.getuid()}"),
            "file": str(realized_allocation_file),
            "file_path_sha256": hashlib.sha256(str(realized_allocation_file).encode()).hexdigest(),
            "file_sha256": plan.input_info["sha256"],
            "file_size_bytes": plan.input_info["size_bytes"],
        },
        "directories": directories,
        "locks": locks,
        "owner_prefix": owner_prefix_record(plan.owner_prefix),
        "records": records,
        "routing_sources": list(realized_routing_sources),
        "schema": "omo-report-preflight-transaction-set/v1",
        "temporary_files": temporary_files,
    }
    return {**transaction, "sha256": hashlib.sha256(canonical_json(transaction).rstrip(b"\n")).hexdigest()}


def validate_committed_route_evidence(plan: Plan, value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise ReceiptError("transaction commitment route evidence is malformed")
    committed: dict[Path, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ReceiptError("transaction commitment route evidence is malformed")
        exists = item.get("exists")
        if type(exists) is not bool:
            raise ReceiptError("transaction commitment route evidence is malformed")
        expected_keys = {"exists", "path", "sha256", "size_bytes"} if exists is True else {"exists", "path"}
        raw_path = item.get("path")
        if set(item) != expected_keys or not isinstance(raw_path, str):
            raise ReceiptError("transaction commitment route evidence is malformed")
        path = Path(raw_path)
        if not path.is_absolute() or path != path.absolute() or path in committed:
            raise ReceiptError("transaction commitment route evidence is malformed")
        if exists is True and (
            not isinstance(item.get("sha256"), str)
            or HASH_RE.fullmatch(str(item["sha256"])) is None
            or not isinstance(item.get("size_bytes"), int)
            or isinstance(item.get("size_bytes"), bool)
            or not 0 <= int(item["size_bytes"]) <= MAX_ROUTE_FILE_BYTES
        ):
            raise ReceiptError("transaction commitment route evidence is malformed")
        committed[path] = dict(item)

    current = {Path(str(item["path"])): item for item in plan.route_evidence}
    if set(committed) != set(current):
        raise ReceiptError("transaction commitment route evidence is inconsistent")
    for path, frozen in committed.items():
        observed = current[path]
        if path != plan.manager:
            if observed != frozen:
                raise ReceiptError("transaction commitment route evidence changed")
            continue

        restored_owner_state: dict[str, object] = {
            "exists": True,
            "path": str(plan.manager),
            "sha256": plan.owner_prefix.sha256,
            "size_bytes": plan.owner_prefix.size_bytes,
        }
        frozen_owner_states: list[dict[str, object]] = [restored_owner_state]
        if plan.owner_prefix.size_bytes == 0 and plan.owner_prefix.sha256 == hashlib.sha256(b"").hexdigest():
            frozen_owner_states.append({"exists": False, "path": str(plan.manager)})
        if frozen not in frozen_owner_states:
            raise ReceiptError("transaction commitment manager route evidence is inconsistent")

        manager_payload = manager_bytes(plan.manager)
        if manager_transaction_state(manager_payload, plan.owner_prefix, plan.pointer) == "invalid":
            raise ReceiptError("manager bytes differ from the bound report transaction")
        owner = manager_payload[: plan.owner_prefix.size_bytes]
        suffix = b"\n" * plan.owner_prefix.separator_bytes + b"(pending)\n" + plan.pointer.encode("utf-8") + b"\n"
        allowed_observed = [
            frozen,
            restored_owner_state,
            {
                "exists": True,
                "path": str(plan.manager),
                "sha256": hashlib.sha256(owner + suffix).hexdigest(),
                "size_bytes": len(owner + suffix),
            },
        ]
        if observed not in allowed_observed:
            raise ReceiptError("transaction commitment manager route evidence changed")
    return tuple(committed[path] for path in sorted(committed, key=str))


def validate_committed_allocation_identity(plan: Plan, file_state: dict[str, object]) -> None:
    committed_dev = file_state.get("dev")
    committed_inode = file_state.get("inode")
    if (
        not isinstance(committed_dev, int)
        or isinstance(committed_dev, bool)
        or committed_dev < 0
        or not isinstance(committed_inode, int)
        or isinstance(committed_inode, bool)
        or committed_inode <= 0
    ):
        raise ReceiptError("transaction commitment allocation identity is malformed")
    committed_identity = (committed_dev, committed_inode)
    if plan.message_identity != committed_identity:
        raise ReceiptError("pending report draft object identity differs from its commitment")
    try:
        held = os.fstat(plan.message_fd)
    except OSError as exc:
        raise ReceiptError("pending report draft object identity changed") from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_uid != os.getuid()
        or (held.st_dev, held.st_ino) != committed_identity
    ):
        raise ReceiptError("pending report draft object identity changed")


def validate_current_allocation_identity(plan: Plan, commitment: dict[str, object]) -> None:
    allocation = commitment.get("allocation")
    file_state = allocation.get("file_at_submission") if isinstance(allocation, dict) else None
    if not isinstance(file_state, dict):
        raise ReceiptError("transaction commitment allocation is malformed")
    validate_committed_allocation_identity(plan, file_state)


def validate_transaction_commitment_bytes(
    plan: Plan,
    payload: bytes,
    *,
    expected_preflight: dict[str, object] | None = None,
    require_current_allocation_identity: bool,
) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("transaction commitment is not valid JSON") from exc
    if not isinstance(parsed, dict) or canonical_json(parsed) != payload:
        raise ReceiptError("transaction commitment is not canonical JSON")
    if set(parsed) != {"allocation", "commitment", "commitment_id", "preflight", "replay_id", "schema"}:
        raise ReceiptError("transaction commitment schema is invalid")
    without_id = dict(parsed)
    commitment_id = without_id.pop("commitment_id", None)
    if (
        parsed.get("schema") != TRANSACTION_COMMITMENT_SCHEMA
        or parsed.get("replay_id") != plan.replay_id
        or not isinstance(commitment_id, str)
        or commitment_id != bound_receipt_id(without_id)
    ):
        raise ReceiptError("transaction commitment identity is invalid")
    preflight = parsed.get("preflight")
    if expected_preflight is not None and preflight != expected_preflight:
        raise ReceiptError("pending report transaction is already bound to a different allocation")
    preflight_allocation = preflight.get("allocation") if isinstance(preflight, dict) else None
    realized_allocation_file = preflight_allocation.get("file") if isinstance(preflight_allocation, dict) else None
    if expected_preflight is None and realized_allocation_file != str(plan.message_path):
        raise ReceiptError("pending report transaction is already bound to a different allocation")
    committed_routing_sources = preflight.get("routing_sources") if isinstance(preflight, dict) else None
    routing_sources = validate_committed_route_evidence(plan, committed_routing_sources)
    if (
        not isinstance(realized_allocation_file, str)
        or not Path(realized_allocation_file).is_absolute()
        or preflight
        != preflight_transaction_set(
            plan,
            allocation_file=Path(realized_allocation_file),
            routing_sources=routing_sources,
        )
    ):
        raise ReceiptError("transaction commitment preflight is inconsistent")
    allocation = parsed.get("allocation")
    if not isinstance(preflight_allocation, dict) or not isinstance(allocation, dict):
        raise ReceiptError("transaction commitment allocation is malformed")
    expected_allocation_keys = {
        "directory",
        "directory_at_submission",
        "file",
        "file_at_submission",
        "helper_allocated",
        "operation",
        "protocol",
    }
    allocation_directory = Path("/tmp") / f"omo-report-drafts-{os.getuid()}"
    allocation_file = allocation.get("file")
    allocation_file_state = allocation.get("file_at_submission")
    allocation_directory_state = allocation.get("directory_at_submission")
    helper_allocated = isinstance(allocation_file, str) and Path(allocation_file).parent == allocation_directory
    if (
        set(allocation) != expected_allocation_keys
        or not isinstance(allocation_file, str)
        or not Path(allocation_file).is_absolute()
        or allocation_file != preflight_allocation.get("file")
        or allocation.get("directory") != str(allocation_directory)
        or allocation.get("operation") != "completed-before-submit"
        or allocation.get("protocol") != "exclusive-create-0600-draft-in-0700-directory"
        or allocation.get("helper_allocated") != helper_allocated
        or not isinstance(allocation_file_state, dict)
        or allocation_file_state.get("exists") is not True
        or allocation_file_state.get("type") != "regular"
        or allocation_file_state.get("uid") != os.getuid()
        or allocation_file_state.get("sha256") != plan.input_info["sha256"]
        or allocation_file_state.get("size") != plan.input_info["size_bytes"]
        or not isinstance(allocation_directory_state, dict)
    ):
        raise ReceiptError("transaction commitment allocation is inconsistent")
    if helper_allocated and (
        allocation_directory_state.get("exists") is not True
        or allocation_directory_state.get("type") != "directory"
        or allocation_directory_state.get("uid") != os.getuid()
        or allocation_directory_state.get("mode") != "0700"
    ):
        raise ReceiptError("transaction commitment allocation directory is inconsistent")
    if require_current_allocation_identity:
        validate_current_allocation_identity(plan, parsed)
    commitment = parsed.get("commitment")
    if (
        not isinstance(commitment, dict)
        or set(commitment) != {"before", "final", "operation", "temporary", "temporary_before"}
        or commitment.get("before") != {"exists": False}
        or commitment.get("final") != str(plan.transaction_commitment_final)
        or commitment.get("operation") != "exclusive-create-fsync-hardlink-unlink-fsync-directory"
        or commitment.get("temporary") != str(plan.transaction_commitment_temporary)
        or commitment.get("temporary_before") != {"exists": False}
    ):
        raise ReceiptError("transaction commitment effect is inconsistent")
    return parsed


def read_transaction_commitment(
    plan: Plan,
    *,
    expected_preflight: dict[str, object] | None = None,
    require_current_allocation_identity: bool,
) -> dict[str, object] | None:
    if not validate_optional_regular(
        plan.transaction_commitment_final,
        "transaction commitment",
        exact_mode=0o600,
    ):
        return None
    payload = regular_file_bytes(
        plan.transaction_commitment_final,
        maximum=MAX_RECEIPT_BYTES,
        field="transaction commitment",
    )
    return validate_transaction_commitment_bytes(
        plan,
        payload,
        expected_preflight=expected_preflight,
        require_current_allocation_identity=require_current_allocation_identity,
    )


def transaction_commitment_bytes(plan: Plan) -> bytes:
    allocation_directory = Path("/tmp") / f"omo-report-drafts-{os.getuid()}"
    record: dict[str, object] = {
        "allocation": {
            "directory": str(allocation_directory),
            "directory_at_submission": path_state(allocation_directory),
            "file": str(plan.message_path),
            "file_at_submission": held_message_state(plan),
            "helper_allocated": plan.message_path.parent == allocation_directory,
            "operation": "completed-before-submit",
            "protocol": "exclusive-create-0600-draft-in-0700-directory",
        },
        "commitment": {
            "before": path_state(plan.transaction_commitment_final),
            "final": str(plan.transaction_commitment_final),
            "operation": "exclusive-create-fsync-hardlink-unlink-fsync-directory",
            "temporary": str(plan.transaction_commitment_temporary),
            "temporary_before": path_state(plan.transaction_commitment_temporary),
        },
        "preflight": preflight_transaction_set(plan),
        "replay_id": plan.replay_id,
        "schema": TRANSACTION_COMMITMENT_SCHEMA,
    }
    payload = canonical_json({**record, "commitment_id": bound_receipt_id(record)})
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ReceiptError("transaction commitment exceeds its size limit")
    _ = validate_transaction_commitment_bytes(plan, payload, require_current_allocation_identity=True)
    return payload


def recover_transaction_commitment_temporary(plan: Plan) -> None:
    if not validate_optional_regular(
        plan.transaction_commitment_temporary,
        "transaction commitment temporary file",
        exact_mode=0o600,
    ):
        return
    if not validate_optional_regular(
        plan.transaction_commitment_final,
        "transaction commitment",
        exact_mode=0o600,
    ):
        raise ReceiptError("incomplete transaction residue exists for transaction commitment temporary file")
    temporary = regular_file_bytes(
        plan.transaction_commitment_temporary,
        maximum=MAX_RECEIPT_BYTES,
        field="transaction commitment temporary file",
    )
    committed = regular_file_bytes(
        plan.transaction_commitment_final,
        maximum=MAX_RECEIPT_BYTES,
        field="transaction commitment",
    )
    temporary_info = plan.transaction_commitment_temporary.lstat()
    committed_info = plan.transaction_commitment_final.lstat()
    if temporary != committed or (temporary_info.st_dev, temporary_info.st_ino) != (committed_info.st_dev, committed_info.st_ino):
        raise ReceiptError("transaction commitment temporary file is ambiguous")
    _ = validate_transaction_commitment_bytes(plan, committed, require_current_allocation_identity=True)
    try:
        plan.transaction_commitment_temporary.unlink()
    except OSError as exc:
        raise ReceiptError("cannot retire transaction commitment temporary name") from exc
    fsync_directory(plan.receipt_directory)


def reject_pending_allocation_rebind(plan: Plan) -> None:
    for candidate in sorted(plan.receipt_directory.glob("*.commitment")):
        if candidate == plan.transaction_commitment_final:
            continue
        replay_id = candidate.name.removesuffix(".commitment")
        if HASH_RE.fullmatch(replay_id) is None:
            raise ReceiptError("transaction commitment filename is invalid")
        payload = regular_file_bytes(candidate, maximum=MAX_RECEIPT_BYTES, field="transaction commitment")
        try:
            record = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptError("transaction commitment is malformed") from exc
        allocation = record.get("allocation") if isinstance(record, dict) else None
        allocation_file = allocation.get("file") if isinstance(allocation, dict) else None
        if allocation_file != str(plan.message_path):
            continue
        receipt = plan.receipt_directory / f"{replay_id}.json"
        if receipt.exists():
            continue
        raise ReceiptError("pending report transaction is already bound to a different allocation")


def allocation_lock_path(plan: Plan) -> Path:
    digest = hashlib.sha256(str(plan.message_path).encode()).hexdigest()
    return plan.receipt_directory / f".allocation-{digest}.lock"


def create_or_reuse_transaction_commitment(plan: Plan) -> dict[str, object]:
    with adjacent_report_lock(allocation_lock_path(plan)):
        existing = read_transaction_commitment(plan, require_current_allocation_identity=True)
        if existing is not None:
            return existing
        reject_pending_allocation_rebind(plan)
        require_absent(plan.transaction_commitment_temporary, "transaction commitment temporary file")
        payload = transaction_commitment_bytes(plan)
        write_new_file(plan.transaction_commitment_temporary, payload, 0o600)
        try:
            os.link(
                plan.transaction_commitment_temporary,
                plan.transaction_commitment_final,
                follow_symlinks=False,
            )
        except FileExistsError:
            try:
                plan.transaction_commitment_temporary.unlink()
            except OSError as exc:
                raise ReceiptError("cannot remove redundant transaction commitment temporary file") from exc
            fsync_directory(plan.receipt_directory)
            existing = read_transaction_commitment(plan, require_current_allocation_identity=True)
            if existing is None:
                raise ReceiptError("transaction commitment disappeared during publication")
            return existing
        except OSError as exc:
            raise ReceiptError("cannot publish transaction commitment") from exc
        try:
            plan.transaction_commitment_temporary.unlink()
        except OSError as exc:
            raise ReceiptError("cannot retire transaction commitment temporary name") from exc
        fsync_directory(plan.receipt_directory)
        readback = regular_file_bytes(
            plan.transaction_commitment_final,
            maximum=MAX_RECEIPT_BYTES,
            field="transaction commitment",
        )
        if readback != payload:
            raise ReceiptError("transaction commitment readback differs from committed bytes")
        return validate_transaction_commitment_bytes(plan, readback, require_current_allocation_identity=True)


def public_preflight_binding(plan: Plan, transaction: dict[str, object] | None = None) -> dict[str, object]:
    transaction = preflight_transaction_set(plan) if transaction is None else transaction
    allocation = transaction["allocation"]
    directories = transaction["directories"]
    locks = transaction["locks"]
    records = transaction["records"]
    routing_sources = transaction["routing_sources"]
    temporary_files = transaction["temporary_files"]
    assert isinstance(allocation, dict) and isinstance(directories, list) and isinstance(locks, dict) and isinstance(records, dict)
    assert isinstance(routing_sources, list) and isinstance(temporary_files, list)
    route_locks = locks["routing"]
    assert isinstance(route_locks, list)
    exact_paths = {
        *(str(path) for path in directories),
        *records.values(),
        *(str(path) for path in temporary_files),
        *(str(item["path"]) for item in routing_sources if isinstance(item, dict)),
        str(allocation["file"]),
        str(locks["acknowledgment"]),
        str(locks["adjacent_report"]),
        str(locks["watcher_authority"]),
        *(str(item["path"]) for item in route_locks if isinstance(item, dict)),
    }
    return {
        "allocation_file_path_sha256": allocation["file_path_sha256"],
        "owner_prefix": transaction["owner_prefix"],
        "path_count": len(exact_paths),
        "route_source_count": len(routing_sources),
        "schema": "omo-report-preflight-binding/v1",
        "sha256": transaction["sha256"],
    }


def description(plan: Plan) -> dict[str, object]:
    validate_helper_snapshot(plan)
    validate_route_snapshot(plan)
    validate_private_layout(plan)
    receipt_payload = read_existing_receipt(plan)
    publication_payload = read_existing_receipt_publication(plan, receipt_payload)
    acknowledgment = None if receipt_payload is not None else read_manager_acknowledgment(plan)
    available, pointer = validate_consistency(plan, receipt_payload, acknowledgment)
    existing_receipt = json.loads(receipt_payload) if receipt_payload is not None else None
    recorded_preflight = existing_receipt.get("preflight") if isinstance(existing_receipt, dict) else None
    if receipt_payload is None:
        commitment = read_transaction_commitment(plan, require_current_allocation_identity=True)
        if commitment is None:
            if pointer != "absent" or (
                acknowledgment is not None
                and not acknowledgment_publication_is_complete(plan, acknowledgment)
            ):
                raise ReceiptError("pending report state has no immutable transaction commitment")
        if commitment is not None:
            recorded_preflight = commitment["preflight"]
    watcher_acknowledged = acknowledgment is not None and pointer == "absent"
    allocation_directory = Path("/tmp") / f"omo-report-drafts-{os.getuid()}"
    return {
        "files": {
            "allocation_directory": str(allocation_directory),
            "manager": str(plan.manager),
            "private_envelope": str(plan.envelope_final),
            "private_receipt": str(plan.receipt_final),
            "receipt_publication": str(plan.receipt_publication_final),
        },
        "input": plan.input_info,
        "locks": {
            "manager_file": str(plan.task_lock),
            "report_transaction": str(plan.report_lock),
        },
        "manager_acknowledgment": {
            "available": available or watcher_acknowledged,
            "required": True,
        },
        "operations": {
            "allocation": "exclusive-create-0600-draft-in-0700-directory-before-description",
            "locks": "producer-create-or-open-and-flock-exclusive; manager-watcher-owns-acknowledgment-lock",
            "manager_acknowledgment": "external-manager-watcher-atomic-replace-consumed-record",
            "manager_file": "atomic-replace-or-create-appended-pointer",
            "private_envelope": "exclusive-create-fsync-rename-fsync-directory-or-verified-reuse",
            "private_receipt": "exclusive-create-fsync-rename-fsync-directory",
            "receipt_publication": "exclusive-create-fsync-rename-fsync-directory",
        },
        "read_only": True,
        "receipt": {
            "available": available,
            "migration_bindings_available": plan.report_context["batch"] is not None and plan.report_context["attempt"] is not None,
            "publication_available": publication_payload is not None,
            "receipt_id": existing_receipt.get("receipt_id") if isinstance(existing_receipt, dict) else None,
            "replay_id": plan.replay_id,
            "support_available": True,
        },
        "routing": public_routing(plan),
        "schema": DESCRIPTION_SCHEMA,
        "status": plan.status,
        "temporary_files": [
            str(plan.envelope_temporary),
            str(plan.manager_temporary),
            str(plan.manager_watcher_temporary),
            str(plan.acknowledgment_temporary),
            str(plan.acknowledgment_maintenance_temporary),
            str(plan.receipt_publication_temporary),
            str(plan.receipt_temporary),
            str(plan.transaction_commitment_temporary),
        ],
        "transaction": public_preflight_binding(plan, recorded_preflight if isinstance(recorded_preflight, dict) else None),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def commit_receipt(plan: Plan, receipt: dict[str, object]) -> bytes:
    if "receipt_id" in receipt:
        raise ReceiptError("receipt identifier was bound more than once")
    receipt = {**receipt, "receipt_id": bound_receipt_id(receipt)}
    payload = canonical_json(receipt)
    if len(payload) > MAX_RECEIPT_BYTES:
        raise ReceiptError("durable receipt exceeds its size limit")
    _ = validate_receipt_bytes(plan, payload)
    require_absent(plan.receipt_temporary, "receipt temporary file")
    require_absent(plan.receipt_final, "receipt final file")
    write_new_file(plan.receipt_temporary, payload, 0o600)
    try:
        os.rename(plan.receipt_temporary, plan.receipt_final)
    except OSError as exc:
        raise ReceiptError("cannot publish durable receipt") from exc
    fsync_directory(plan.receipt_directory)
    readback = regular_file_bytes(plan.receipt_final, maximum=MAX_RECEIPT_BYTES, field="durable receipt")
    if readback != payload:
        raise ReceiptError("durable receipt readback differs from committed bytes")
    _ = validate_receipt_bytes(plan, readback)
    return readback


def validate_receipt_publication_bytes(plan: Plan, payload: bytes, receipt_payload: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(payload)
        receipt = json.loads(receipt_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("receipt publication record is not valid JSON") from exc
    if not isinstance(parsed, dict) or canonical_json(parsed) != payload:
        raise ReceiptError("receipt publication record is not canonical JSON")
    if set(parsed) != {"publication_id", "receipt_id", "receipt_path", "receipt_state", "replay_id", "schema"}:
        raise ReceiptError("receipt publication record schema is invalid")
    without_id = dict(parsed)
    publication_id = without_id.pop("publication_id", None)
    if not isinstance(publication_id, str) or publication_id != bound_receipt_id(without_id):
        raise ReceiptError("receipt publication content binding is invalid")
    if (
        not isinstance(receipt, dict)
        or parsed.get("schema") != RECEIPT_PUBLICATION_SCHEMA
        or parsed.get("receipt_id") != receipt.get("receipt_id")
        or parsed.get("receipt_path") != str(plan.receipt_final)
        or parsed.get("replay_id") != plan.replay_id
        or parsed.get("receipt_state") != path_state(plan.receipt_final)
    ):
        raise ReceiptError("receipt publication record is inconsistent")
    return parsed


def receipt_publication_payload(plan: Plan, receipt: dict[str, object]) -> bytes:
    record: dict[str, object] = {
        "receipt_id": receipt["receipt_id"],
        "receipt_path": str(plan.receipt_final),
        "receipt_state": path_state(plan.receipt_final),
        "replay_id": plan.replay_id,
        "schema": RECEIPT_PUBLICATION_SCHEMA,
    }
    return canonical_json({**record, "publication_id": bound_receipt_id(record)})


def finish_receipt_publication(plan: Plan, payload: bytes, receipt_payload: bytes) -> bytes:
    try:
        os.rename(plan.receipt_publication_temporary, plan.receipt_publication_final)
    except OSError as exc:
        raise ReceiptError("cannot publish receipt publication record") from exc
    return confirm_receipt_publication(plan, payload, receipt_payload)


def confirm_receipt_publication(plan: Plan, payload: bytes, receipt_payload: bytes) -> bytes:
    """Durably confirm an exact final publication, including its directory entry."""

    fsync_directory(plan.receipt_directory)
    readback = regular_file_bytes(
        plan.receipt_publication_final,
        maximum=MAX_RECEIPT_BYTES,
        field="receipt publication record",
    )
    if readback != payload:
        raise ReceiptError("receipt publication readback differs from committed bytes")
    _ = validate_receipt_publication_bytes(plan, readback, receipt_payload)
    _ = validate_receipt_bytes(plan, receipt_payload)
    return readback


def commit_receipt_publication(plan: Plan, receipt_payload: bytes) -> bytes:
    receipt = json.loads(receipt_payload)
    if not isinstance(receipt, dict):
        raise ReceiptError("durable receipt is malformed during publication")
    payload = receipt_publication_payload(plan, receipt)
    require_absent(plan.receipt_publication_temporary, "receipt publication temporary file")
    require_absent(plan.receipt_publication_final, "receipt publication final file")
    write_new_file(plan.receipt_publication_temporary, payload, 0o600)
    return finish_receipt_publication(plan, payload, receipt_payload)


def recover_receipt_publication(plan: Plan, receipt_payload: bytes) -> bytes:
    receipt = json.loads(receipt_payload)
    if not isinstance(receipt, dict):
        raise ReceiptError("durable receipt is malformed during publication recovery")
    expected = receipt_publication_payload(plan, receipt)
    if validate_optional_regular(
        plan.receipt_publication_temporary,
        "receipt publication temporary file",
        exact_mode=0o600,
    ):
        temporary = regular_file_bytes(
            plan.receipt_publication_temporary,
            maximum=MAX_RECEIPT_BYTES,
            field="receipt publication temporary file",
        )
        if temporary == expected:
            return finish_receipt_publication(plan, expected, receipt_payload)
        try:
            plan.receipt_publication_temporary.unlink()
        except OSError as exc:
            raise ReceiptError("cannot discard incomplete receipt publication") from exc
        fsync_directory(plan.receipt_directory)
    return commit_receipt_publication(plan, receipt_payload)


def read_existing_receipt_publication(plan: Plan, receipt_payload: bytes | None) -> bytes | None:
    if not validate_optional_regular(
        plan.receipt_publication_final,
        "receipt publication record",
        exact_mode=0o600,
    ):
        return None
    if receipt_payload is None:
        raise ReceiptError("receipt publication record has no durable receipt")
    payload = regular_file_bytes(
        plan.receipt_publication_final,
        maximum=MAX_RECEIPT_BYTES,
        field="receipt publication record",
    )
    _ = validate_receipt_publication_bytes(plan, payload, receipt_payload)
    return payload


def signal_manager_acknowledgment_publication(plan: Plan, receipt_payload: bytes) -> None:
    """Release a still-live watcher authority after exact receipt publication."""

    receipt = json.loads(receipt_payload)
    effects = receipt.get("side_effects") if isinstance(receipt, dict) else None
    acknowledgment = effects.get("manager_acknowledgment") if isinstance(effects, dict) else None
    if not isinstance(acknowledgment, dict):
        return
    transition = acknowledgment.get("transition")
    authority = transition.get("authority") if isinstance(transition, dict) else None
    if not isinstance(authority, dict):
        return
    pid = authority.get("pid")
    start_ticks = authority.get("process_start_ticks")
    lock_dev = authority.get("lock_dev")
    lock_inode = authority.get("lock_inode")
    source_path = authority.get("source_path")
    source_sha256 = authority.get("source_sha256")
    token_sha256 = authority.get("token_sha256")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(start_ticks, int)
        or isinstance(start_ticks, bool)
        or not isinstance(lock_dev, int)
        or isinstance(lock_dev, bool)
        or not isinstance(lock_inode, int)
        or isinstance(lock_inode, bool)
        or not isinstance(source_path, str)
        or not isinstance(source_sha256, str)
        or not isinstance(token_sha256, str)
    ):
        raise ReceiptError("durable receipt authority cannot be released")
    if not watcher_report_authority_is_live(
        pid=pid,
        start_ticks=start_ticks,
        lock_path=plan.acknowledgment_authority_lock,
        lock_dev=lock_dev,
        lock_inode=lock_inode,
        source_path=Path(source_path),
        source_sha256=source_sha256,
        token_sha256=token_sha256,
    ):
        return
    entry_digest = acknowledgment.get("entry_sha256")
    if not isinstance(entry_digest, str) or HASH_RE.fullmatch(entry_digest) is None:
        raise ReceiptError("durable receipt authority release binding is malformed")
    payload = f"{entry_digest}\n".encode()
    if validate_optional_regular(
        plan.acknowledgment_authority_completion,
        "manager acknowledgment authority completion",
        exact_mode=0o600,
    ):
        existing = regular_file_bytes(
            plan.acknowledgment_authority_completion,
            maximum=256,
            field="manager acknowledgment authority completion",
        )
        if existing != payload:
            raise ReceiptError("manager acknowledgment authority completion is inconsistent")
        return
    write_new_file(plan.acknowledgment_authority_completion, payload, 0o600)
    fsync_directory(plan.acknowledgment_authority_completion.parent)


def submit(plan: Plan) -> tuple[bytes, bytes] | None:
    validate_helper_snapshot(plan)
    manager_directory_before = path_state(plan.manager.parent)
    envelope_directory_before = path_state(plan.envelope_directory)
    acknowledgment_state_before = path_state(plan.acknowledgment_state)
    acknowledgment_lock_before = path_state(plan.acknowledgment_lock)
    acknowledgment_temporary_before = path_state(plan.acknowledgment_temporary)
    acknowledgment_maintenance_temporary_before = path_state(plan.acknowledgment_maintenance_temporary)
    acknowledgment_authority_directory_before = path_state(plan.acknowledgment_authority_lock.parent)
    acknowledgment_authority_lock_before = path_state(plan.acknowledgment_authority_lock)
    acknowledgment_authority_completion_before = path_state(plan.acknowledgment_authority_completion)
    report_lock_before = path_state(plan.report_lock)
    task_lock_directory_before = path_state(plan.task_lock.parent)
    route_lock_before = {target: path_state(lock_path) for target, lock_path in plan.route_locks}
    receipt_state_home = plan.receipt_directory.parent.parent
    receipt_application_directory = plan.receipt_directory.parent
    receipt_state_home_before = path_state(receipt_state_home)
    receipt_application_directory_before = path_state(receipt_application_directory)
    receipt_directory_before = path_state(plan.receipt_directory)
    receipt_before = path_state(plan.receipt_final)
    receipt_temporary_before = path_state(plan.receipt_temporary)
    publication_before = path_state(plan.receipt_publication_final)
    publication_temporary_before = path_state(plan.receipt_publication_temporary)
    with adjacent_report_lock(plan.report_lock):
        with ExitStack() as route_locks:
            for _, lock_path in plan.route_locks:
                route_locks.enter_context(task_file_lock_at_path(lock_path))
            validate_helper_snapshot(plan)
            validate_route_snapshot(plan)
            prepare_private_directories(plan)
            recover_transaction_commitment_temporary(plan)
            validate_private_layout(plan, allow_publication_recovery=True)
            validate_lock_file(plan.report_lock, "adjacent report lock")
            for _, lock_path in plan.route_locks:
                validate_lock_file(lock_path, "route transaction lock")
            receipt_payload = read_existing_receipt(plan)
            publication_payload = read_existing_receipt_publication(plan, receipt_payload)
            acknowledgment = None if receipt_payload is not None else read_manager_acknowledgment(plan)
            available, pointer = validate_consistency(plan, receipt_payload, acknowledgment)
            if available:
                if receipt_payload is None:
                    raise ReceiptError("receipt availability changed unexpectedly")
                if publication_payload is None:
                    publication_payload = recover_receipt_publication(plan, receipt_payload)
                else:
                    publication_payload = confirm_receipt_publication(plan, publication_payload, receipt_payload)
                signal_manager_acknowledgment_publication(plan, receipt_payload)
                return receipt_payload, publication_payload
            commitment = read_transaction_commitment(plan, require_current_allocation_identity=True)
            if commitment is None:
                if pointer != "absent" or (
                    acknowledgment is not None
                    and not acknowledgment_publication_is_complete(plan, acknowledgment)
                ):
                    raise ReceiptError("pending report state has no immutable transaction commitment")
                commitment = create_or_reuse_transaction_commitment(plan)
            if pointer != "absent":
                acknowledgment = None
            manager_before = path_state(plan.manager)
            manager_temporary_before = path_state(plan.manager_temporary)
            manager_watcher_temporary_before = path_state(plan.manager_watcher_temporary)
            envelope_before = path_state(plan.envelope_final)
            envelope_temporary_before = path_state(plan.envelope_temporary)
            stamp = datetime.now().astimezone().strftime("%H:%M")
            validate_current_allocation_identity(plan, commitment)
            envelope_operation = create_or_reuse_envelope(plan, stamp)
            if acknowledgment is not None and pointer != "active":
                manager_operation = "external-manager-acknowledged-no-active-pointer"
            else:
                manager_operation = append_or_deduplicate_manager(plan)
            _ = require_valid_envelope(plan)
            committed_pointer = pointer_state(manager_bytes(plan.manager), plan)
            if acknowledgment is None and committed_pointer != "active":
                raise ReceiptError("committed manager pointer is not watcher-compatible")
            validate_helper_snapshot(plan)
            validate_route_snapshot(plan, ignore=frozenset({plan.manager}))
            if acknowledgment is None:
                acknowledgment = wait_for_manager_acknowledgment(plan)
            acknowledged_pointer = pointer_state(manager_bytes(plan.manager), plan)
            if acknowledgment is None or acknowledged_pointer != "absent":
                return None
            validate_current_allocation_identity(plan, commitment)
            acknowledgment_state_after = path_state(plan.acknowledgment_state)
            validate_private_layout(plan)
            committed_allocation = commitment.get("allocation")
            committed_transaction = commitment.get("commitment")
            committed_preflight = commitment.get("preflight")
            committed_routing_sources = committed_preflight.get("routing_sources") if isinstance(committed_preflight, dict) else None
            if (
                not isinstance(committed_allocation, dict)
                or not isinstance(committed_transaction, dict)
                or not isinstance(committed_preflight, dict)
                or not isinstance(committed_routing_sources, list)
            ):
                raise ReceiptError("transaction commitment is malformed during receipt construction")
            receipt_routing = {
                **plan.routing,
                "route_evidence": committed_routing_sources,
                "route_evidence_sha256": hashlib.sha256(canonical_json(committed_routing_sources).rstrip(b"\n")).hexdigest(),
            }

            effects: dict[str, object] = {
                "durable_receipt": {
                    "after": {
                        "attested_by": str(plan.receipt_publication_final),
                    },
                    "application_directory": str(receipt_application_directory),
                    "application_directory_after": path_state(receipt_application_directory),
                    "application_directory_before": receipt_application_directory_before,
                    "before": receipt_before,
                    "directory": str(plan.receipt_directory),
                    "directory_after_before_record": path_state(plan.receipt_directory),
                    "directory_before": receipt_directory_before,
                    "final": str(plan.receipt_final),
                    "operation": "write-fsync-rename-fsync-directory",
                    "state_home": str(receipt_state_home),
                    "state_home_after": path_state(receipt_state_home),
                    "state_home_before": receipt_state_home_before,
                    "temporary": str(plan.receipt_temporary),
                    "temporary_after": path_state(plan.receipt_temporary),
                    "temporary_before": receipt_temporary_before,
                },
                "locks": {
                    "adjacent_report": {
                        "after": path_state(plan.report_lock),
                        "before": report_lock_before,
                        "directory": str(plan.report_lock.parent),
                        "directory_after": path_state(plan.report_lock.parent),
                        "directory_before": manager_directory_before,
                        "operation": "create-or-open-and-flock-exclusive",
                        "path": str(plan.report_lock),
                    },
                    "task_file": {
                        "after": path_state(plan.task_lock),
                        "before": route_lock_before[plan.manager],
                        "directory": str(plan.task_lock.parent),
                        "directory_after": path_state(plan.task_lock.parent),
                        "directory_before": task_lock_directory_before,
                        "operation": "create-or-open-and-flock-exclusive",
                        "path": str(plan.task_lock),
                    },
                    "route_evidence": [
                        {
                            "after": path_state(lock_path),
                            "before": route_lock_before[target],
                            "directory": str(lock_path.parent),
                            "directory_after": path_state(lock_path.parent),
                            "directory_before": task_lock_directory_before,
                            "operation": "create-or-open-and-flock-exclusive",
                            "path": str(lock_path),
                            "source": str(target),
                        }
                        for target, lock_path in plan.route_locks
                        if target != plan.manager
                    ],
                },
                "manager_acknowledgment": {
                    **acknowledgment,
                    "authority_completion": str(plan.acknowledgment_authority_completion),
                    "authority_completion_before": acknowledgment_authority_completion_before,
                    "authority_directory": str(plan.acknowledgment_authority_lock.parent),
                    "authority_directory_after": path_state(plan.acknowledgment_authority_lock.parent),
                    "authority_directory_before": acknowledgment_authority_directory_before,
                    "authority_lock": str(plan.acknowledgment_authority_lock),
                    "authority_lock_after": path_state(plan.acknowledgment_authority_lock),
                    "authority_lock_before": acknowledgment_authority_lock_before,
                    "lock": str(plan.acknowledgment_lock),
                    "lock_after": path_state(plan.acknowledgment_lock),
                    "lock_before": acknowledgment_lock_before,
                    "maintenance_temporary": str(plan.acknowledgment_maintenance_temporary),
                    "maintenance_temporary_after": path_state(plan.acknowledgment_maintenance_temporary),
                    "maintenance_temporary_before": acknowledgment_maintenance_temporary_before,
                    "operation": "external-manager-watcher-atomic-replace-consumed-record",
                    "state_after": acknowledgment_state_after,
                    "state_before": acknowledgment_state_before,
                    "temporary": str(plan.acknowledgment_temporary),
                    "temporary_after": path_state(plan.acknowledgment_temporary),
                    "temporary_before": acknowledgment_temporary_before,
                },
                "manager_file": {
                    "after": path_state(plan.manager),
                    "before": manager_before,
                    "directory": str(plan.manager.parent),
                    "directory_after": path_state(plan.manager.parent),
                    "directory_before": manager_directory_before,
                    "operation": manager_operation,
                    "owner_prefix": owner_prefix_record(plan.owner_prefix),
                    "path": str(plan.manager),
                    "record_pointer": plan.pointer,
                    "temporary": str(plan.manager_temporary),
                    "temporary_after": path_state(plan.manager_temporary),
                    "temporary_before": manager_temporary_before,
                    "watcher_temporary": str(plan.manager_watcher_temporary),
                    "watcher_temporary_after": path_state(plan.manager_watcher_temporary),
                    "watcher_temporary_before": manager_watcher_temporary_before,
                },
                "private_allocation": {
                    **committed_allocation,
                    "transaction_commitment": {
                        **committed_transaction,
                        "after": path_state(plan.transaction_commitment_final),
                        "commitment_id": commitment["commitment_id"],
                        "temporary_after": path_state(plan.transaction_commitment_temporary),
                    },
                },
                "private_envelope": {
                    "after": path_state(plan.envelope_final),
                    "before": envelope_before,
                    "directory": str(plan.envelope_directory),
                    "directory_after": path_state(plan.envelope_directory),
                    "directory_before": envelope_directory_before,
                    "final": str(plan.envelope_final),
                    "operation": envelope_operation,
                    "temporary": str(plan.envelope_temporary),
                    "temporary_after": path_state(plan.envelope_temporary),
                    "temporary_before": envelope_temporary_before,
                },
                "receipt_publication": {
                    "after": {
                        "exact_state_returned_by": "omo-report-acceptance/v1",
                    },
                    "before": publication_before,
                    "final": str(plan.receipt_publication_final),
                    "operation": "write-fsync-rename-fsync-directory",
                    "temporary": str(plan.receipt_publication_temporary),
                    "temporary_after": path_state(plan.receipt_publication_temporary),
                    "temporary_before": publication_temporary_before,
                },
            }
            receipt: dict[str, object] = {
                "accepted": True,
                "accepted_at_utc": utc_now(),
                "helper": plan.helper,
                "input": plan.input_info,
                "preflight": committed_preflight,
                "receipt_record": receipt_record(plan),
                "replay_id": plan.replay_id,
                "report_context": plan.report_context,
                "routing": receipt_routing,
                "schema": RECEIPT_SCHEMA,
                "side_effects": effects,
                "status": plan.status,
            }
            receipt_payload = commit_receipt(plan, receipt)
            publication_payload = commit_receipt_publication(plan, receipt_payload)
            signal_manager_acknowledgment_publication(plan, receipt_payload)
            return receipt_payload, publication_payload


def acceptance_output(plan: Plan, receipt_payload: bytes, publication_payload: bytes) -> dict[str, object]:
    receipt = json.loads(receipt_payload)
    publication = json.loads(publication_payload)
    return {
        "accepted": True,
        "accepted_at_utc": receipt["accepted_at_utc"],
        "input": plan.input_info,
        "manager_acknowledged": True,
        "publication_id": publication["publication_id"],
        "publication_path": str(plan.receipt_publication_final),
        "publication_state": path_state(plan.receipt_publication_final),
        "receipt_id": receipt["receipt_id"],
        "receipt_path": str(plan.receipt_final),
        "receipt_state": publication["receipt_state"],
        "replay_id": plan.replay_id,
        "routing": public_routing(plan),
        "schema": ACCEPTANCE_SCHEMA,
        "status": plan.status,
    }


def pending_output(plan: Plan) -> dict[str, object]:
    return {
        "accepted": False,
        "input": plan.input_info,
        "manager_acknowledged": False,
        "replay_id": plan.replay_id,
        "routing": public_routing(plan),
        "schema": ACCEPTANCE_SCHEMA,
        "status": plan.status,
    }


def run(argv: list[str] | None = None) -> bytes:
    args = parse_args(argv)
    for attempt in range(32):
        plan = build_plan(args)
        try:
            if plan.mode == "describe":
                return canonical_json(description(plan))
            try:
                result = submit(plan)
            except ReceiptError as exc:
                owner_changed_before_mutation = (
                    str(exc) == "manager pointer and bound owner bytes are inconsistent"
                    and not plan.envelope_final.exists()
                )
                if not owner_changed_before_mutation or attempt == 31:
                    raise
                continue
            if result is None:
                return canonical_json(pending_output(plan))
            receipt_payload, publication_payload = result
            return canonical_json(acceptance_output(plan, receipt_payload, publication_payload))
        finally:
            os.close(plan.message_fd)
    raise AssertionError("owner-prefix rebind loop exhausted")


def main() -> int:
    try:
        output = run()
    except (ReceiptError, OSError, ValueError) as exc:
        print(f"omo_report_receipt.py: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
