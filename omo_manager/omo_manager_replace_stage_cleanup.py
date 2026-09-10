#!/usr/bin/env python3
"""Remove authenticated displaced-inode receipts from committed manager replacements."""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

from omo_manager.omo_manager_replace import RENAME_NOREPLACE
from omo_manager.omo_repository_custody import publish_or_validate

SCHEMA = "omo-manager-replace-stage-cleanup/v1"
REVIEW_SCHEMA = "omo-manager-replace-stage-cleanup-review/v1"
AUDIT_SCHEMA = "omo-manager-replace-stage-cleanup-audit/v1"
STAGE_RE = re.compile(r"^\.(?P<task>[^/]+)\.omo-manager-replace-stage-[0-9a-f]{32}$")
LIBC = ctypes.CDLL(None, use_errno=True)


class CleanupError(RuntimeError):
    """A cleanup invariant failed before any unverified removal."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def move_noreplace(source_parent_fd: int, source: str, destination_parent_fd: int, destination: str) -> None:
    result = LIBC.renameat2(
        source_parent_fd,
        ctypes.c_char_p(os.fsencode(source)),
        destination_parent_fd,
        ctypes.c_char_p(os.fsencode(destination)),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{source}->{destination}")


def open_private_directory(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    details = os.fstat(descriptor)
    linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or (linked.st_dev, linked.st_ino) != (details.st_dev, details.st_ino):
        os.close(descriptor)
        raise CleanupError(f"replacement cleanup custody directory is invalid: {name}")
    if created:
        os.fchmod(descriptor, 0o700)
        details = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o700 or (linked.st_dev, linked.st_ino) != (details.st_dev, details.st_ino):
        os.close(descriptor)
        raise CleanupError(f"replacement cleanup custody directory is invalid: {name}")
    return descriptor


def validate_linked_directory(parent_fd: int | None, name: str | Path, descriptor: int, label: str) -> None:
    held = os.fstat(descriptor)
    linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(linked.st_mode) or held.st_uid != os.getuid() or (held.st_dev, held.st_ino) != (linked.st_dev, linked.st_ino):
        raise CleanupError(f"{label} directory identity drifted")


def validate_custody_file(parent_fd: int, name: str, raw: dict[str, object]) -> None:
    descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        details = os.fstat(descriptor)
        chunks: list[bytes] = []
        offset = 0
        while chunk := os.pread(descriptor, 1024 * 1024, offset):
            chunks.append(chunk)
            offset += len(chunk)
        data = b"".join(chunks)
        expected = identity(Path(string(raw.get("path"), "replacement receipt path")), data, details)
        if expected != raw or not stat.S_ISREG(details.st_mode):
            raise CleanupError(f"replacement stage receipt custody drifted: {name}")
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (linked.st_dev, linked.st_ino) != (details.st_dev, details.st_ino):
            raise CleanupError(f"replacement stage receipt custody rebound: {name}")
    finally:
        os.close(descriptor)


def read_exact(path: Path, expected_sha256: str, label: str, *, private: bool = False) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute() or path.is_symlink():
        raise CleanupError(f"{label} is not one absolute regular file")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
        raise CleanupError(f"{label} has invalid ownership or type")
    if private and stat.S_IMODE(before.st_mode) != 0o600:
        raise CleanupError(f"{label} is not owner-private")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CleanupError(f"{label} changed while read")
    if digest(data) != expected_sha256:
        raise CleanupError(f"{label} SHA-256 differs")
    return data, after


def read_current(path: Path, label: str) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute() or path.is_symlink():
        raise CleanupError(f"{label} is not one absolute regular file")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
        raise CleanupError(f"{label} has invalid ownership or type")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CleanupError(f"{label} changed while read")
    return data, after


def identity(path: Path, data: bytes, details: os.stat_result) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": digest(data),
        "device": details.st_dev,
        "inode": details.st_ino,
        "mode": stat.S_IMODE(details.st_mode),
        "uid": details.st_uid,
        "gid": details.st_gid,
        "size_bytes": details.st_size,
    }


def object_map(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CleanupError(f"{label} is invalid")
    return value


def string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CleanupError(f"{label} is invalid")
    return value


def decode(value: object, label: str) -> bytes:
    try:
        if not isinstance(value, str):
            raise ValueError
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise CleanupError(f"{label} is not canonical base64") from exc


def validate_audits(audit_specs: tuple[tuple[Path, str], ...]) -> tuple[Path, list[dict[str, object]], dict[str, list[bytes]]]:
    root: Path | None = None
    audits: list[dict[str, object]] = []
    before_by_task: dict[str, list[bytes]] = {}
    for path, expected_sha256 in audit_specs:
        data, details = read_exact(path, expected_sha256, "replacement audit", private=True)
        record = object_map(json.loads(data), "replacement audit")
        committed = record.pop("record_sha256", None)
        if committed != digest(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()):
            raise CleanupError("replacement audit integrity commitment changed")
        record["record_sha256"] = committed
        if record.get("operation") != "manager-replace" or record.get("state") != "committed":
            raise CleanupError("replacement audit is not committed")
        candidate_root = Path(string(record.get("root"), "replacement root")).resolve(strict=True)
        if root is None:
            root = candidate_root
        elif root != candidate_root:
            raise CleanupError("replacement audits do not share one root")
        files = record.get("files")
        completed = record.get("completed_writes")
        if not isinstance(files, list) or not isinstance(completed, list):
            raise CleanupError("replacement audit file set is invalid")
        if {string(object_map(item, "replacement file").get("task"), "replacement task") for item in files} != set(completed):
            raise CleanupError("replacement audit does not bind every committed write")
        for raw in files:
            entry = object_map(raw, "replacement file")
            task = string(entry.get("task"), "replacement task")
            _ = decode(entry.get("after"), f"{task} after bytes")
            before = entry.get("before")
            if before is not None:
                before_by_task.setdefault(task, []).append(decode(before, f"{task} before bytes"))
        audits.append(identity(path, data, details))
    if root is None:
        raise CleanupError("at least one replacement audit is required")
    return root, audits, before_by_task


def discover_receipts(root: Path, before_by_task: dict[str, list[bytes]]) -> list[dict[str, object]]:
    matched: list[dict[str, object]] = []
    remaining = {task: list(values) for task, values in before_by_task.items()}
    for path in sorted(root.iterdir()):
        found = STAGE_RE.fullmatch(path.name)
        if found is None:
            continue
        task = found.group("task")
        if task not in remaining:
            continue
        data, details = read_current(path, "replacement stage receipt")
        candidates = remaining.get(task, [])
        indexes = [index for index, value in enumerate(candidates) if value == data]
        if len(indexes) != 1:
            raise CleanupError(f"replacement stage receipt is not uniquely authenticated: {path.name}")
        del candidates[indexes[0]]
        matched.append(identity(path, data, details))
    unresolved = [task for task, values in remaining.items() for _value in values]
    if unresolved:
        raise CleanupError(f"committed displaced-inode receipt is missing: {', '.join(unresolved)}")
    if len(matched) != sum(len(values) for values in before_by_task.values()):
        raise CleanupError("replacement stage receipt set is incomplete")
    return matched


def validate_identity(record: dict[str, object], label: str) -> tuple[Path, bytes, os.stat_result]:
    path = Path(string(record.get("path"), f"{label} path"))
    data, details = read_exact(path, string(record.get("sha256"), f"{label} SHA-256"), label)
    if identity(path, data, details) != record:
        raise CleanupError(f"{label} identity drifted")
    return path, data, details


def load_packet(path: Path, expected_sha256: str, *, require_receipts: bool = True) -> dict[str, object]:
    data, _ = read_exact(path, expected_sha256, "cleanup packet", private=True)
    packet = object_map(json.loads(data), "cleanup packet")
    if packet.get("schema") != SCHEMA or packet.get("operation") != "remove-committed-displaced-inode-receipts":
        raise CleanupError("cleanup packet schema is invalid")
    audits = packet.get("audits")
    receipts = packet.get("receipts")
    if not isinstance(audits, list) or not isinstance(receipts, list) or not receipts:
        raise CleanupError("cleanup packet bindings are invalid")
    audit_specs: list[tuple[Path, str]] = []
    for raw in audits:
        binding = object_map(raw, "replacement audit binding")
        audit_path, _data, _details = validate_identity(binding, "replacement audit")
        audit_specs.append((audit_path, string(binding.get("sha256"), "replacement audit SHA-256")))
    root, _audit_bindings, before_by_task = validate_audits(tuple(audit_specs))
    if packet.get("root") != str(root):
        raise CleanupError("cleanup packet replacement root is invalid")
    expected: dict[tuple[str, str], int] = {}
    for task, values in before_by_task.items():
        for value in values:
            key = task, digest(value)
            expected[key] = expected.get(key, 0) + 1
    observed: dict[tuple[str, str], int] = {}
    for raw in receipts:
        binding = object_map(raw, "replacement receipt binding")
        receipt_path = Path(string(binding.get("path"), "replacement receipt path"))
        found = STAGE_RE.fullmatch(receipt_path.name)
        if receipt_path.parent != root or found is None:
            raise CleanupError("cleanup packet contains an invalid receipt path")
        key = found.group("task"), string(binding.get("sha256"), "replacement receipt SHA-256")
        observed[key] = observed.get(key, 0) + 1
        if require_receipts:
            _ = validate_identity(binding, "replacement stage receipt")
    if observed != expected:
        raise CleanupError("cleanup packet receipt set differs from committed audit bytes")
    return packet


def prepare(args: argparse.Namespace) -> None:
    audit_specs = tuple(zip(args.audit, args.audit_sha256, strict=True))
    root, audits, before_by_task = validate_audits(audit_specs)
    receipts = discover_receipts(root, before_by_task)
    packet = {
        "schema": SCHEMA,
        "operation": "remove-committed-displaced-inode-receipts",
        "root": str(root),
        "audits": audits,
        "receipts": receipts,
        "audit_output": str(args.audit_output.resolve(strict=False)),
    }
    publish_or_validate(args.output.resolve(strict=False), canonical(packet), "manager replacement stage cleanup packet")
    print(digest(canonical(packet)))


def review(args: argparse.Namespace) -> None:
    _ = load_packet(args.packet, args.packet_sha256)
    report = {"schema": REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": args.packet_sha256}
    publish_or_validate(args.review_output.resolve(strict=False), canonical(report), "manager replacement stage cleanup review")


def execute(args: argparse.Namespace) -> None:
    packet = load_packet(args.packet, args.packet_sha256, require_receipts=False)
    review_data, _ = read_exact(args.review_report, args.review_report_sha256, "cleanup review", private=True)
    review_record = object_map(json.loads(review_data), "cleanup review")
    if review_record != {"schema": REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": args.packet_sha256}:
        raise CleanupError("independent review does not PASS this packet")
    raw_receipts = packet.get("receipts")
    if not isinstance(raw_receipts, list):
        raise CleanupError("cleanup receipt bindings are invalid")
    receipts = [object_map(item, "replacement receipt binding") for item in raw_receipts]
    prepared = {
        "schema": AUDIT_SCHEMA,
        "state": "prepared",
        "packet_sha256": args.packet_sha256,
        "review_sha256": args.review_report_sha256,
        "receipts": receipts,
    }
    audit_output = Path(string(packet.get("audit_output"), "cleanup audit output"))
    prepared_path = Path(f"{audit_output}.prepared")
    publish_or_validate(prepared_path, canonical(prepared), "prepared manager replacement stage cleanup audit")
    root = Path(string(packet.get("root"), "replacement root"))
    packet_paths = {Path(string(raw.get("path"), "replacement receipt path")) for raw in receipts}
    selected_tasks = {match.group("task") for path in packet_paths if (match := STAGE_RE.fullmatch(path.name)) is not None}
    selected_live = {path for path in root.iterdir() if (match := STAGE_RE.fullmatch(path.name)) is not None and match.group("task") in selected_tasks}
    if not selected_live <= packet_paths:
        raise CleanupError("selected replacement stage namespace drifted")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    validate_linked_directory(None, root, root_fd, "replacement root")
    git_fd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), dir_fd=root_fd)
    git_details = os.fstat(git_fd)
    if not stat.S_ISDIR(git_details.st_mode) or git_details.st_uid != os.getuid():
        os.close(git_fd)
        os.close(root_fd)
        raise CleanupError("replacement root Git directory is invalid")
    custody_root_fd = open_private_directory(git_fd, "omo-manager-replace-cleanup")
    custody_fd = open_private_directory(custody_root_fd, args.packet_sha256)
    custody_dir = root / ".git" / "omo-manager-replace-cleanup" / args.packet_sha256
    try:
        for index, raw in enumerate(receipts):
            path = Path(string(raw.get("path"), "replacement receipt path"))
            custody_name = f"{index:03d}-{path.name}"
            path_exists = True
            try:
                _ = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                path_exists = False
            try:
                _ = os.stat(custody_name, dir_fd=custody_fd, follow_symlinks=False)
                custody_exists = True
            except FileNotFoundError:
                custody_exists = False
            if path_exists and custody_exists:
                raise CleanupError(f"replacement stage receipt and custody copy both exist: {path.name}")
            if not path_exists and not custody_exists:
                raise CleanupError(f"replacement stage receipt custody is missing: {path.name}")
            if custody_exists:
                validate_custody_file(custody_fd, custody_name, raw)
                continue
            path, _data, details = validate_identity(raw, "replacement stage receipt")
            linked = os.stat(path.name, dir_fd=root_fd, follow_symlinks=False)
            if (linked.st_dev, linked.st_ino) != (details.st_dev, details.st_ino):
                raise CleanupError(f"replacement stage receipt rebound: {path.name}")
            move_noreplace(root_fd, path.name, custody_fd, custody_name)
            moved = os.stat(custody_name, dir_fd=custody_fd, follow_symlinks=False)
            if (moved.st_dev, moved.st_ino) != (details.st_dev, details.st_ino):
                raise CleanupError(f"replacement stage receipt raced during cleanup: {path.name}")
            os.fsync(root_fd)
            os.fsync(custody_fd)
        for index, raw in enumerate(receipts):
            path = Path(string(raw.get("path"), "replacement receipt path"))
            validate_custody_file(custody_fd, f"{index:03d}-{path.name}", raw)
        validate_linked_directory(None, root, root_fd, "replacement root")
        validate_linked_directory(root_fd, ".git", git_fd, "replacement root Git")
        validate_linked_directory(git_fd, "omo-manager-replace-cleanup", custody_root_fd, "cleanup custody root")
        validate_linked_directory(custody_root_fd, args.packet_sha256, custody_fd, "cleanup packet custody")
        if any((match := STAGE_RE.fullmatch(name)) is not None and match.group("task") in selected_tasks for name in os.listdir(root_fd)):
            raise CleanupError("selected replacement stage cleanup did not reach absence")
        complete = dict(prepared)
        complete["state"] = "complete"
        complete["prepared_sha256"] = digest(canonical(prepared))
        complete["custody_directory"] = str(custody_dir)
        publish_or_validate(audit_output, canonical(complete), "complete manager replacement stage cleanup audit")
    finally:
        os.close(custody_fd)
        os.close(custody_root_fd)
        os.close(git_fd)
        os.close(root_fd)
    print(f"removed {len(receipts)} authenticated replacement stage receipt(s); audit={audit_output}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    modes = result.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--review", action="store_true")
    modes.add_argument("--execute", action="store_true")
    result.add_argument("--audit", action="append", type=Path, default=[])
    result.add_argument("--audit-sha256", action="append", default=[])
    result.add_argument("--audit-output", type=Path)
    result.add_argument("--output", type=Path)
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
            if not args.audit or len(args.audit) != len(args.audit_sha256) or args.output is None or args.audit_output is None:
                raise CleanupError("prepare requires matched audits, output, and audit output")
            prepare(args)
        elif args.review:
            if args.packet is None or args.review_output is None:
                raise CleanupError("review requires packet and review output")
            review(args)
        else:
            if args.packet is None or args.review_report is None:
                raise CleanupError("execute requires packet and review report")
            execute(args)
    except (CleanupError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"omo_manager_replace_stage_cleanup.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
