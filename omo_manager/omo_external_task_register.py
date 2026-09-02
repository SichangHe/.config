#!/usr/bin/env python3
"""Register one unchanged external task path for watcher discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_task_lock import canonical_target
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_file_lock_at_path
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_manager_env import external_task_registry_dir
from omo_manager.omo_manager_env import manager_state_dir

PLAN_SCHEMA = "omo-external-task-registration-plan/v2"
RECEIPT_SCHEMA = "omo-external-task-registration/v2"
ROLLBACK_SCHEMA = "omo-external-task-registration-rollback/v1"
INVALIDATION_SCHEMA = "omo-external-task-registration-invalidation/v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RUNAT_RE = re.compile(r"^runat:\s+([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?)\s*$", re.MULTILINE)
MAX_FILE_BYTES = 8_000_000


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    path: Path
    data: bytes
    device: int
    inode: int
    parent_device: int
    parent_inode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class RegistrationPlan:
    schema: str
    root: str
    root_device: int
    root_inode: int
    source_root: str
    source_root_device: int
    source_root_inode: int
    task_ref: str
    task: str
    task_device: int
    task_inode: int
    task_parent_device: int
    task_parent_inode: int
    task_sha256: str
    todo: str
    todo_device: int
    todo_inode: int
    todo_sha256: str
    todo_section: str
    todo_line: str
    runat: str
    managerat: str
    owner_set_sha256: str
    registry: str
    registry_device: int
    registry_inode: int
    registry_sha256: str
    key: str


@dataclass(frozen=True)
class ExternalTaskRegistration:
    schema: str
    plan_sha256: str
    root: str
    root_device: int
    root_inode: int
    source_root: str
    source_root_device: int
    source_root_inode: int
    task_ref: str
    task: str
    task_device: int
    task_inode: int
    task_parent_device: int
    task_parent_inode: int
    task_sha256: str
    todo: str
    todo_device: int
    todo_inode: int
    todo_sha256: str
    todo_section: str
    todo_line: str
    runat: str
    managerat: str
    owner_set_sha256: str
    registry: str
    registry_device: int
    registry_inode: int
    registry_sha256: str
    key: str


@dataclass(frozen=True)
class RegistrationRollback:
    schema: str
    key: str
    receipt: str
    receipt_sha256: str


@dataclass(frozen=True)
class RegistrationInvalidation:
    schema: str
    key: str
    receipt: str
    receipt_sha256: str
    reason: str


@dataclass(frozen=True)
class RegisteredExternalTask:
    task: str
    section: str
    line: str
    target: str


def default_state_dir() -> Path:
    return manager_state_dir()


def default_registry_dir() -> Path:
    return external_task_registry_dir()


def assert_authoritative_registry(registry: Path) -> None:
    expected = absolute_normal_path(default_registry_dir(), "configured external task registry")
    if registry != expected:
        raise RegistrationError("external task registry is not the configured authoritative ledger")


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def absolute_normal_path(path: Path, label: str) -> Path:
    if not path.is_absolute() or str(path) != os.path.normpath(str(path)):
        raise RegistrationError(f"{label} must be an absolute normalized path")
    return path


def open_directory(path: Path, label: str, *, private: bool = False) -> tuple[int, os.stat_result]:
    absolute_normal_path(path, label)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or (private and stat.S_IMODE(info.st_mode) != 0o700):
            raise RegistrationError(f"{label} has unsafe ownership or mode")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def read_file_at(
    parent_fd: int,
    parent_info: os.stat_result,
    path: Path,
    label: str,
    *,
    private: bool = False,
    maximum_bytes: int = MAX_FILE_BYTES,
) -> Snapshot:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & (0o077 if private else 0o022):
            raise RegistrationError(f"{label} has unsafe type, ownership, or mode")
        data = b""
        while chunk := os.read(fd, min(65_536, maximum_bytes + 1 - len(data))):
            data += chunk
            if len(data) > maximum_bytes:
                raise RegistrationError(f"{label} exceeds {maximum_bytes} bytes")
        after = os.fstat(fd)
        bound = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or (bound.st_dev, bound.st_ino) != (before.st_dev, before.st_ino):
            raise RegistrationError(f"{label} changed while read")
        return Snapshot(path, data, before.st_dev, before.st_ino, parent_info.st_dev, parent_info.st_ino)
    finally:
        if fd >= 0:
            os.close(fd)


def read_file(path: Path, label: str, *, private: bool = False, maximum_bytes: int = MAX_FILE_BYTES) -> Snapshot:
    absolute_normal_path(path, label)
    parent_fd, parent_info = open_directory(path.parent, f"{label} parent", private=private)
    try:
        return read_file_at(parent_fd, parent_info, path, label, private=private, maximum_bytes=maximum_bytes)
    finally:
        os.close(parent_fd)


def registry_entries(registry: Path, *, exclude: set[str] | None = None) -> tuple[tuple[str, bytes], ...]:
    directory_fd, info = open_directory(registry, "external task registry", private=True)
    try:
        names_before = sorted(name for name in os.listdir(directory_fd) if name != ".lock" and name not in (exclude or set()))
        if any(not (name.startswith("registration-") or name.startswith("rollback-") or name.startswith("invalidation-")) or not name.endswith(".json") for name in names_before):
            raise RegistrationError("external task registry contains an unknown entry")
        snapshots = tuple((name, read_file_at(directory_fd, info, registry / name, "external task registry entry", private=True)) for name in names_before)
        names_after = sorted(name for name in os.listdir(directory_fd) if name != ".lock" and name not in (exclude or set()))
        if names_after != names_before:
            raise RegistrationError("external task registry changed while read")
        for name, snapshot in snapshots:
            current = read_file_at(directory_fd, info, registry / name, "external task registry entry", private=True)
            if (current.device, current.inode, current.data) != (snapshot.device, snapshot.inode, snapshot.data):
                raise RegistrationError("external task registry entry changed while read")
        return tuple((name, snapshot.data) for name, snapshot in snapshots)
    finally:
        os.close(directory_fd)


def registry_entries_sha256(entries: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for name, data in entries:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def registry_sha256(registry: Path, *, exclude: set[str] | None = None) -> str:
    return registry_entries_sha256(registry_entries(registry, exclude=exclude))


def strict_object(data: bytes, expected: set[str], label: str) -> dict[str, object]:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise RegistrationError(f"{label} has duplicate fields")
            result[key] = value
        return result

    try:
        value: object = json.loads(data, object_pairs_hook=unique_object)
    except RegistrationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistrationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != expected or not all(isinstance(key, str) for key in value):
        raise RegistrationError(f"{label} has wrong or duplicate fields")
    return value


def plan_fields() -> set[str]:
    return set(RegistrationPlan.__dataclass_fields__)


def receipt_fields() -> set[str]:
    return set(ExternalTaskRegistration.__dataclass_fields__)


def rollback_fields() -> set[str]:
    return set(RegistrationRollback.__dataclass_fields__)


def invalidation_fields() -> set[str]:
    return set(RegistrationInvalidation.__dataclass_fields__)


PLAN_STRING_FIELDS = {
    "schema",
    "root",
    "source_root",
    "task_ref",
    "task",
    "task_sha256",
    "todo",
    "todo_sha256",
    "todo_section",
    "todo_line",
    "runat",
    "managerat",
    "owner_set_sha256",
    "registry",
    "registry_sha256",
    "key",
}
PLAN_INTEGER_FIELDS = {
    "root_device",
    "root_inode",
    "source_root_device",
    "source_root_inode",
    "task_device",
    "task_inode",
    "task_parent_device",
    "task_parent_inode",
    "todo_device",
    "todo_inode",
    "registry_device",
    "registry_inode",
}


def validate_plan_value_types(values: dict[str, object], label: str) -> None:
    if any(not isinstance(values[field], str) for field in PLAN_STRING_FIELDS) or any(
        not isinstance(values[field], int) or isinstance(values[field], bool) or values[field] < 0 for field in PLAN_INTEGER_FIELDS
    ):
        raise RegistrationError(f"{label} field types are invalid")


def parse_plan(data: bytes) -> RegistrationPlan:
    values = strict_object(data, plan_fields(), "registration plan")
    validate_plan_value_types(values, "registration plan")
    try:
        plan = RegistrationPlan(**values)
    except TypeError as exc:
        raise RegistrationError("registration plan field types are invalid") from exc
    if (
        plan.schema != PLAN_SCHEMA
        or SHA256_RE.fullmatch(plan.key) is None
        or any(SHA256_RE.fullmatch(value) is None for value in (plan.task_sha256, plan.todo_sha256, plan.owner_set_sha256, plan.registry_sha256))
    ):
        raise RegistrationError("registration plan schema or key is invalid")
    for value, label in (
        (plan.root, "watcher root"),
        (plan.source_root, "external source root"),
        (plan.task, "external task"),
        (plan.todo, "external source TODO"),
        (plan.registry, "external task registry"),
    ):
        absolute_normal_path(Path(value), label)
    validate_plan_structure(plan)
    expected_key = hashlib.sha256(canonical_bytes({key: value for key, value in asdict(plan).items() if key != "key"})).hexdigest()
    if plan.key != expected_key:
        raise RegistrationError("registration plan key is invalid")
    if data != canonical_bytes(asdict(plan)):
        raise RegistrationError("registration plan is not canonical")
    return plan


def validate_plan_structure(plan: RegistrationPlan) -> None:
    root = Path(plan.root)
    source_root = Path(plan.source_root)
    task = Path(plan.task)
    if (
        normalized_task_path(source_root, plan.task_ref) != task
        or root == task
        or root in task.parents
        or source_root == root
        or source_root not in task.parents
        or Path(plan.todo) != source_root / "TODO.md"
        or canonical_target(plan.runat) == canonical_target(plan.managerat)
    ):
        raise RegistrationError("registration plan path, TODO, or target structure is invalid")


def parse_receipt(data: bytes) -> ExternalTaskRegistration:
    values = strict_object(data, receipt_fields(), "registration receipt")
    validate_plan_value_types({key: value for key, value in values.items() if key != "plan_sha256"}, "registration receipt")
    if not isinstance(values["plan_sha256"], str):
        raise RegistrationError("registration receipt field types are invalid")
    try:
        receipt = ExternalTaskRegistration(**values)
    except TypeError as exc:
        raise RegistrationError("registration receipt field types are invalid") from exc
    if receipt.schema != RECEIPT_SCHEMA or SHA256_RE.fullmatch(receipt.key) is None or SHA256_RE.fullmatch(receipt.plan_sha256) is None:
        raise RegistrationError("registration receipt schema or digest is invalid")
    plan_values = asdict(receipt)
    plan_values.pop("plan_sha256")
    plan_values["schema"] = PLAN_SCHEMA
    plan = parse_plan(canonical_bytes(plan_values))
    if hashlib.sha256(canonical_bytes(asdict(plan))).hexdigest() != receipt.plan_sha256:
        raise RegistrationError("registration receipt does not bind its plan bytes")
    if data != canonical_bytes(asdict(receipt)):
        raise RegistrationError("registration receipt is not canonical")
    return receipt


def parse_rollback(data: bytes) -> RegistrationRollback:
    values = strict_object(data, rollback_fields(), "registration rollback")
    if not all(isinstance(value, str) for value in values.values()):
        raise RegistrationError("registration rollback field types are invalid")
    rollback = RegistrationRollback(**values)
    if rollback.schema != ROLLBACK_SCHEMA or SHA256_RE.fullmatch(rollback.key) is None or SHA256_RE.fullmatch(rollback.receipt_sha256) is None:
        raise RegistrationError("registration rollback schema or digest is invalid")
    if data != canonical_bytes(asdict(rollback)):
        raise RegistrationError("registration rollback is not canonical")
    return rollback


def parse_invalidation(data: bytes) -> RegistrationInvalidation:
    values = strict_object(data, invalidation_fields(), "registration invalidation")
    if not all(isinstance(value, str) for value in values.values()):
        raise RegistrationError("registration invalidation field types are invalid")
    invalidation = RegistrationInvalidation(**values)
    if (
        invalidation.schema != INVALIDATION_SCHEMA
        or SHA256_RE.fullmatch(invalidation.key) is None
        or SHA256_RE.fullmatch(invalidation.receipt_sha256) is None
        or invalidation.reason != "source changed during registration publication"
    ):
        raise RegistrationError("registration invalidation schema, digest, or reason is invalid")
    if data != canonical_bytes(asdict(invalidation)):
        raise RegistrationError("registration invalidation is not canonical")
    return invalidation


def normalized_task_path(root: Path, task_ref: str) -> Path:
    if not task_ref or "\n" in task_ref or "\r" in task_ref or not task_ref.endswith(".md"):
        raise RegistrationError("task reference must be one Markdown path")
    value = Path(task_ref)
    candidate = value if value.is_absolute() else root / value
    return Path(os.path.normpath(str(candidate)))


def task_metadata(snapshot: Snapshot, root: Path, runat: str, managerat: str) -> None:
    try:
        metadata = parse_task_metadata(snapshot.data.decode(), root)
    except (UnicodeDecodeError, TaskFrontmatterError) as exc:
        raise RegistrationError("external task metadata is invalid") from exc
    if (
        metadata is None
        or metadata.status != "blocked"
        or metadata.blocked_on != "human"
        or metadata.runat != runat
        or metadata.managerat != managerat
        or metadata.is_manager
        or metadata.tool != "codex"
    ):
        raise RegistrationError("external task lifecycle, blocker, owner, manager, or tool drifted")


def todo_membership(todo: Snapshot, task_ref: str, runat: str) -> tuple[str, str] | None:
    try:
        text = todo.data.decode()
    except UnicodeDecodeError:
        return None
    from omo_manager.omo_agent_status import parse_task_text

    matching = [task for task in parse_task_text(text) if task.task_file == task_ref]
    if len(matching) != 1 or matching[0].target != runat or matching[0].section not in {"todo:current", "todo:human pending", "todo:low priority"}:
        return None
    return matching[0].section, matching[0].line


def active_runat_claim(data: bytes, root: Path, target: str) -> bool:
    try:
        text = data.decode()
    except UnicodeDecodeError:
        return False
    raw = any(canonical_target(match.group(1)) == canonical_target(target) for match in RUNAT_RE.finditer(text.partition("---\n")[2].partition("\n---")[0]))
    try:
        metadata = parse_task_metadata(text, root)
    except TaskFrontmatterError:
        return raw
    return metadata is not None and metadata.status != "done" and canonical_target(metadata.runat) == canonical_target(target)


def owner_claims(roots: tuple[Path, ...], target: str) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for root in roots:
        todo = read_file(root / "TODO.md", "task ownership TODO")
        try:
            text = todo.data.decode()
        except UnicodeDecodeError as exc:
            raise RegistrationError("task ownership TODO is not UTF-8") from exc
        from omo_manager.omo_agent_status import parse_task_text

        for task in parse_task_text(text):
            if task.section not in {"todo:current", "todo:human pending", "todo:low priority"}:
                continue
            candidate = normalized_task_path(root, task.task_file)
            if root != candidate and root not in candidate.parents:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_symlink():
                raise RegistrationError("task ownership scan contains a symlink")
            try:
                snapshot = read_file(candidate, "task ownership candidate")
            except FileNotFoundError:
                continue
            if active_runat_claim(snapshot.data, root, target):
                claims.append((str(candidate), snapshot.sha256))
    return sorted(claims)


def owner_set_sha256(roots: tuple[Path, ...], target: str) -> str:
    claims = owner_claims(roots, target)
    return hashlib.sha256(canonical_bytes(claims)).hexdigest()


def active_receipts(registry: Path) -> tuple[ExternalTaskRegistration, ...]:
    entries = registry_entries(registry)
    registry_fd, registry_info = open_directory(registry, "external task registry", private=True)
    os.close(registry_fd)
    registrations = {name: data for name, data in entries if name.startswith("registration-")}
    receipts: dict[str, ExternalTaskRegistration] = {}
    for name, data in registrations.items():
        receipt = parse_receipt(data)
        if name != f"registration-{receipt.key}.json" or receipt.registry != str(registry) or (receipt.registry_device, receipt.registry_inode) != (registry_info.st_dev, registry_info.st_ino):
            raise RegistrationError("registration receipt filename or registry identity is inconsistent")
        receipts[name] = receipt
    deactivations: dict[str, tuple[str, bytes]] = {}
    for name, data in entries:
        if not (name.startswith("rollback-") or name.startswith("invalidation-")):
            continue
        if name.startswith("rollback-"):
            deactivation = parse_rollback(data)
            expected_name = f"rollback-{deactivation.key}.json"
        else:
            deactivation = parse_invalidation(data)
            expected_name = f"invalidation-{deactivation.key}.json"
        if name != expected_name:
            raise RegistrationError("registration deactivation filename is inconsistent")
        registration_name = f"registration-{deactivation.key}.json"
        receipt_data = registrations.get(registration_name)
        if (
            receipt_data is None
            or deactivation.receipt != str(registry / registration_name)
            or hashlib.sha256(receipt_data).hexdigest() != deactivation.receipt_sha256
            or registration_name in deactivations
        ):
            raise RegistrationError("registration deactivation does not bind one receipt")
        deactivations[registration_name] = (name, data)

    committed: dict[str, bytes] = {}
    remaining = dict(receipts)
    active: list[ExternalTaskRegistration] = []
    while remaining:
        prior_digest = registry_entries_sha256(tuple(sorted(committed.items())))
        candidates = [(name, receipt) for name, receipt in remaining.items() if receipt.registry_sha256 == prior_digest]
        if len(candidates) != 1:
            raise RegistrationError("external task registry history is ambiguous or disconnected")
        name, receipt = candidates[0]
        committed[name] = registrations[name]
        remaining.pop(name)
        deactivation = deactivations.get(name)
        if deactivation is None:
            active.append(receipt)
        else:
            committed[deactivation[0]] = deactivation[1]
    if committed != dict(entries):
        raise RegistrationError("external task registry history is incomplete")
    return tuple(active)


def assert_no_registration_conflict(registry: Path, root: Path, task: Path, task_ref: str, target: str) -> None:
    for receipt in active_receipts(registry):
        if not receipt_is_current(receipt):
            continue
        same_task = receipt.task == str(task) or (receipt.root == str(root) and receipt.task_ref == task_ref)
        same_target = canonical_target(receipt.runat) == canonical_target(target)
        if same_task or same_target:
            raise RegistrationError("external task registration conflicts with an active receipt")


def build_plan(
    root: Path,
    source_root: Path,
    task: Path,
    task_ref: str,
    runat: str,
    managerat: str,
    task_sha256: str,
    todo_sha256: str,
    registry: Path,
) -> RegistrationPlan:
    root = absolute_normal_path(root, "watcher root")
    source_root = absolute_normal_path(source_root, "external source root")
    task = absolute_normal_path(task, "external task")
    registry = absolute_normal_path(registry, "external task registry")
    assert_authoritative_registry(registry)
    if normalized_task_path(source_root, task_ref) != task or root == source_root or root == task or root in task.parents or source_root not in task.parents:
        raise RegistrationError("source task reference must resolve outside the watcher root")
    if canonical_target(runat) == canonical_target(managerat):
        raise RegistrationError("external task owner and manager must differ")
    if SHA256_RE.fullmatch(task_sha256) is None or SHA256_RE.fullmatch(todo_sha256) is None:
        raise RegistrationError("task and TODO SHA-256 values must be lowercase hexadecimal")
    root_fd, root_info = open_directory(root, "watcher root")
    os.close(root_fd)
    source_root_fd, source_root_info = open_directory(source_root, "external source root")
    os.close(source_root_fd)
    registry_fd, registry_info = open_directory(registry, "external task registry", private=True)
    os.close(registry_fd)
    task_snapshot = read_file(task, "external task")
    todo_snapshot = read_file(source_root / "TODO.md", "external source TODO")
    if task_snapshot.sha256 != task_sha256 or todo_snapshot.sha256 != todo_sha256:
        raise RegistrationError("external task or TODO bytes do not match the expected digest")
    task_metadata(task_snapshot, source_root, runat, managerat)
    membership = todo_membership(todo_snapshot, task_ref, runat)
    if membership is None:
        raise RegistrationError("TODO must contain exactly one active external task entry with the exact owner target")
    ownership_roots = tuple(dict.fromkeys((source_root, root)))
    claims = owner_claims(ownership_roots, runat)
    if claims != [(str(task), task_snapshot.sha256)]:
        raise RegistrationError("external target does not have exactly one active task owner")
    owner_digest = hashlib.sha256(canonical_bytes(claims)).hexdigest()
    assert_no_registration_conflict(registry, root, task, task_ref, runat)
    values = {
        "schema": PLAN_SCHEMA,
        "root": str(root),
        "root_device": root_info.st_dev,
        "root_inode": root_info.st_ino,
        "source_root": str(source_root),
        "source_root_device": source_root_info.st_dev,
        "source_root_inode": source_root_info.st_ino,
        "task_ref": task_ref,
        "task": str(task),
        "task_device": task_snapshot.device,
        "task_inode": task_snapshot.inode,
        "task_parent_device": task_snapshot.parent_device,
        "task_parent_inode": task_snapshot.parent_inode,
        "task_sha256": task_snapshot.sha256,
        "todo": str(source_root / "TODO.md"),
        "todo_device": todo_snapshot.device,
        "todo_inode": todo_snapshot.inode,
        "todo_sha256": todo_snapshot.sha256,
        "todo_section": membership[0],
        "todo_line": membership[1],
        "runat": runat,
        "managerat": managerat,
        "owner_set_sha256": owner_digest,
        "registry": str(registry),
        "registry_device": registry_info.st_dev,
        "registry_inode": registry_info.st_ino,
        "registry_sha256": registry_sha256(registry),
    }
    key = hashlib.sha256(canonical_bytes(values)).hexdigest()
    return RegistrationPlan(**values, key=key)


def assert_plan_current(plan: RegistrationPlan) -> tuple[Snapshot, Snapshot]:
    root = Path(plan.root)
    source_root = Path(plan.source_root)
    task = Path(plan.task)
    registry = Path(plan.registry)
    root_fd, root_info = open_directory(root, "watcher root")
    os.close(root_fd)
    source_root_fd, source_root_info = open_directory(source_root, "external source root")
    os.close(source_root_fd)
    registry_fd, registry_info = open_directory(registry, "external task registry", private=True)
    os.close(registry_fd)
    task_snapshot = read_file(task, "external task")
    todo_snapshot = read_file(Path(plan.todo), "work-log TODO")
    identities = (
        (root_info.st_dev, root_info.st_ino, plan.root_device, plan.root_inode),
        (source_root_info.st_dev, source_root_info.st_ino, plan.source_root_device, plan.source_root_inode),
        (registry_info.st_dev, registry_info.st_ino, plan.registry_device, plan.registry_inode),
        (task_snapshot.device, task_snapshot.inode, plan.task_device, plan.task_inode),
        (task_snapshot.parent_device, task_snapshot.parent_inode, plan.task_parent_device, plan.task_parent_inode),
        (todo_snapshot.device, todo_snapshot.inode, plan.todo_device, plan.todo_inode),
    )
    if any((actual_dev, actual_inode) != (expected_dev, expected_inode) for actual_dev, actual_inode, expected_dev, expected_inode in identities):
        raise RegistrationError("registration path identity changed after prepare")
    if task_snapshot.sha256 != plan.task_sha256 or todo_snapshot.sha256 != plan.todo_sha256:
        raise RegistrationError("external task or TODO bytes changed after prepare")
    task_metadata(task_snapshot, source_root, plan.runat, plan.managerat)
    ownership_roots = tuple(dict.fromkeys((source_root, root)))
    if (
        todo_membership(todo_snapshot, plan.task_ref, plan.runat) != (plan.todo_section, plan.todo_line)
        or owner_set_sha256(ownership_roots, plan.runat) != plan.owner_set_sha256
        or owner_claims(ownership_roots, plan.runat) != [(str(task), task_snapshot.sha256)]
    ):
        raise RegistrationError("TODO membership or active target ownership changed after prepare")
    return task_snapshot, todo_snapshot


def plan_from_receipt(receipt: ExternalTaskRegistration) -> RegistrationPlan:
    values = asdict(receipt)
    values.pop("plan_sha256")
    values["schema"] = PLAN_SCHEMA
    return RegistrationPlan(**values)


def receipt_is_current(receipt: ExternalTaskRegistration) -> bool:
    try:
        _ = assert_plan_current(plan_from_receipt(receipt))
        return True
    except (OSError, RegistrationError):
        return False


def publish_no_replace(directory: Path, name: str, data: bytes) -> Path:
    directory_fd, _info = open_directory(directory, "external task registry", private=True)
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
    fd = -1
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return directory / name


def initialize_registry(registry: Path) -> Path:
    """Create only the exact private registry leaf, or validate an existing one."""

    registry = absolute_normal_path(registry, "external task registry")
    assert_authoritative_registry(registry)
    parent_fd, _parent_info = open_directory(registry.parent, "external task registry parent", private=True)
    created = False
    registry_fd = -1
    try:
        try:
            os.mkdir(registry.name, 0o700, dir_fd=parent_fd)
            created = True
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        registry_fd = os.open(registry.name, flags, dir_fd=parent_fd)
        info = os.fstat(registry_fd)
        bound = os.stat(registry.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or (info.st_dev, info.st_ino) != (bound.st_dev, bound.st_ino):
            raise RegistrationError("external task registry has unsafe ownership, mode, or identity")
    finally:
        if registry_fd >= 0:
            os.close(registry_fd)
        os.close(parent_fd)
    if created and registry_entries(registry):
        raise RegistrationError("new external task registry is not empty")
    return registry


def receipt_for_plan(plan: RegistrationPlan, plan_sha256: str) -> ExternalTaskRegistration:
    values = asdict(plan)
    values["schema"] = RECEIPT_SCHEMA
    values["plan_sha256"] = plan_sha256
    return ExternalTaskRegistration(**values)


def registration_lock(plan: RegistrationPlan) -> ExitStack:
    stack = ExitStack()
    try:
        for root in sorted({Path(plan.root), Path(plan.source_root)}, key=str):
            stack.enter_context(task_file_lock(root / ".omo-task-membership.lock"))
            stack.enter_context(task_target_lock(root, plan.runat))
        stack.enter_context(task_file_lock(Path(plan.task)))
        stack.enter_context(task_file_lock_at_path(Path(plan.registry) / ".lock"))
        return stack
    except BaseException:
        stack.close()
        raise


def apply_plan(plan_path: Path, plan_sha256: str) -> Path:
    plan_snapshot = read_file(plan_path, "registration plan", private=True)
    if plan_snapshot.sha256 != plan_sha256:
        raise RegistrationError("registration plan does not match its expected digest")
    plan = parse_plan(plan_snapshot.data)
    assert_authoritative_registry(Path(plan.registry))
    receipt = receipt_for_plan(plan, plan_sha256)
    receipt_data = canonical_bytes(asdict(receipt))
    receipt_name = f"registration-{plan.key}.json"
    registry = Path(plan.registry)
    with registration_lock(plan):
        existing_path = registry / receipt_name
        try:
            existing = read_file(existing_path, "registration receipt", private=True)
        except FileNotFoundError:
            existing = None
        excluded = {receipt_name} if existing is not None else set()
        if registry_sha256(registry, exclude=excluded) != plan.registry_sha256:
            raise RegistrationError("external task registry changed after prepare")
        _ = assert_plan_current(plan)
        if existing is not None:
            if existing.data != receipt_data:
                raise RegistrationError("existing registration receipt conflicts with the plan")
            return existing_path
        assert_no_registration_conflict(registry, Path(plan.root), Path(plan.task), plan.task_ref, plan.runat)
        receipt_path = publish_no_replace(registry, receipt_name, receipt_data)
        try:
            _ = assert_plan_current(plan)
        except (OSError, RegistrationError) as exc:
            invalidation = RegistrationInvalidation(
                INVALIDATION_SCHEMA,
                plan.key,
                str(receipt_path),
                hashlib.sha256(receipt_data).hexdigest(),
                "source changed during registration publication",
            )
            invalidation_data = canonical_bytes(asdict(invalidation))
            invalidation_path = registry / f"invalidation-{plan.key}.json"
            try:
                publish_no_replace(registry, invalidation_path.name, invalidation_data)
            except FileExistsError:
                if read_file(invalidation_path, "registration invalidation", private=True).data != invalidation_data:
                    raise RegistrationError("conflicting registration invalidation already exists") from exc
            raise RegistrationError("registration source changed during publication; receipt was invalidated") from exc
        return receipt_path


def rollback_registration(receipt_path: Path, receipt_sha256: str) -> Path:
    receipt_snapshot = read_file(receipt_path, "registration receipt", private=True)
    if receipt_snapshot.sha256 != receipt_sha256:
        raise RegistrationError("registration receipt does not match its expected digest")
    receipt = parse_receipt(receipt_snapshot.data)
    if receipt_path != Path(receipt.registry) / f"registration-{receipt.key}.json":
        raise RegistrationError("registration receipt path is inconsistent")
    plan = plan_from_receipt(receipt)
    rollback_name = f"rollback-{receipt.key}.json"
    rollback = canonical_bytes(
        {
            "schema": ROLLBACK_SCHEMA,
            "key": receipt.key,
            "receipt": str(receipt_path),
            "receipt_sha256": receipt_sha256,
        }
    )
    registry = Path(receipt.registry)
    assert_authoritative_registry(registry)
    with registration_lock(plan):
        existing_path = registry / rollback_name
        try:
            existing = read_file(existing_path, "registration rollback", private=True)
        except FileNotFoundError:
            existing = None
        excluded = {receipt_path.name, rollback_name} if existing is not None else {receipt_path.name}
        if registry_sha256(registry, exclude=excluded) != receipt.registry_sha256:
            raise RegistrationError("external task registry changed since registration")
        _ = assert_plan_current(plan)
        current_receipt = read_file(receipt_path, "registration receipt", private=True)
        if current_receipt.data != receipt_snapshot.data:
            raise RegistrationError("registration receipt changed before rollback")
        if existing is not None:
            if existing.data != rollback:
                raise RegistrationError("existing rollback conflicts with the registration")
            return existing_path
        return publish_no_replace(registry, rollback_name, rollback)


def resolve_registered_external_task(root: Path, task_ref: str, candidate: Path, registry: Path | None = None) -> Path | None:
    registry = registry or default_registry_dir()
    try:
        assert_authoritative_registry(registry)
        root_fd, root_info = open_directory(root, "work-log root")
        os.close(root_fd)
        matches: list[ExternalTaskRegistration] = []
        active = [receipt for receipt in active_receipts(registry) if receipt_is_current(receipt)]
        active_targets = [canonical_target(receipt.runat) for receipt in active]
        active_tasks = [receipt.task for receipt in active]
        if len(active_targets) != len(set(active_targets)) or len(active_tasks) != len(set(active_tasks)):
            return None
        for receipt in active:
            if receipt.root != str(root) or task_ref not in {receipt.task_ref, receipt.task} or receipt.task != str(candidate):
                continue
            if (root_info.st_dev, root_info.st_ino) != (receipt.root_device, receipt.root_inode):
                continue
            snapshot = read_file(candidate, "registered external task")
            todo = read_file(Path(receipt.todo), "registered work-log TODO")
            if (
                (snapshot.device, snapshot.inode) != (receipt.task_device, receipt.task_inode)
                or (snapshot.parent_device, snapshot.parent_inode) != (receipt.task_parent_device, receipt.task_parent_inode)
                or snapshot.sha256 != receipt.task_sha256
                or (todo.device, todo.inode) != (receipt.todo_device, receipt.todo_inode)
                or todo.sha256 != receipt.todo_sha256
                or todo_membership(todo, receipt.task_ref, receipt.runat) != (receipt.todo_section, receipt.todo_line)
            ):
                continue
            source_root = Path(receipt.source_root)
            task_metadata(snapshot, source_root, receipt.runat, receipt.managerat)
            if owner_set_sha256(tuple(dict.fromkeys((source_root, root))), receipt.runat) != receipt.owner_set_sha256:
                continue
            matches.append(receipt)
        return candidate if len(matches) == 1 else None
    except (OSError, RegistrationError):
        return None


# 🧑 Human: "Continue working and complete them"
def registered_external_tasks(root: Path, registry: Path | None = None) -> tuple[RegisteredExternalTask, ...]:
    """Return current external-root memberships registered to one watcher root."""

    registry = registry or default_registry_dir()
    try:
        assert_authoritative_registry(registry)
        root_fd, root_info = open_directory(root, "watcher root")
        os.close(root_fd)
        active = [receipt for receipt in active_receipts(registry) if receipt_is_current(receipt)]
        targets = [canonical_target(receipt.runat) for receipt in active]
        tasks = [receipt.task for receipt in active]
        if len(targets) != len(set(targets)) or len(tasks) != len(set(tasks)):
            return ()
        return tuple(
            RegisteredExternalTask(receipt.task, receipt.todo_section, receipt.todo_line, receipt.runat)
            for receipt in active
            if receipt.root == str(root) and (receipt.root_device, receipt.root_inode) == (root_info.st_dev, root_info.st_ino)
        )
    except (OSError, RegistrationError):
        return ()


def write_plan(path: Path, plan: RegistrationPlan) -> None:
    data = canonical_bytes(asdict(plan))
    parent_fd, _info = open_directory(path.parent, "registration plan parent", private=True)
    os.close(parent_fd)
    try:
        publish_no_replace(path.parent, path.name, data)
    except FileExistsError as exc:
        raise RegistrationError("registration plan output already exists") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize_parser = subparsers.add_parser("init-registry")
    _ = initialize_parser.add_argument("--registry", type=Path, default=default_registry_dir())
    for command in ("dry-run", "prepare"):
        operation = subparsers.add_parser(command)
        _ = operation.add_argument("--root", type=Path, required=True)
        _ = operation.add_argument("--source-root", type=Path, required=True)
        _ = operation.add_argument("--task", type=Path, required=True)
        _ = operation.add_argument("--task-ref", required=True)
        _ = operation.add_argument("--runat", required=True)
        _ = operation.add_argument("--managerat", required=True)
        _ = operation.add_argument("--task-sha256", required=True)
        _ = operation.add_argument("--todo-sha256", required=True)
        _ = operation.add_argument("--registry", type=Path, default=default_registry_dir())
        if command == "prepare":
            _ = operation.add_argument("--output", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply")
    _ = apply_parser.add_argument("--plan", type=Path, required=True)
    _ = apply_parser.add_argument("--plan-sha256", required=True)
    rollback_parser = subparsers.add_parser("rollback")
    _ = rollback_parser.add_argument("--receipt", type=Path, required=True)
    _ = rollback_parser.add_argument("--receipt-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-registry":
            print(initialize_registry(args.registry))
            return 0
        if args.command in {"dry-run", "prepare"}:
            plan = build_plan(args.root, args.source_root, args.task, args.task_ref, args.runat, args.managerat, args.task_sha256, args.todo_sha256, args.registry)
            if args.command == "dry-run":
                print(canonical_bytes(asdict(plan)).decode(), end="")
            else:
                write_plan(args.output, plan)
                print(args.output)
            return 0
        if args.command == "apply":
            print(apply_plan(args.plan, args.plan_sha256))
            return 0
        print(rollback_registration(args.receipt, args.receipt_sha256))
        return 0
    except (OSError, RegistrationError) as exc:
        print(f"omo_external_task_register.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
