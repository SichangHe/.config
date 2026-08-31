"""Validate one external capability for exact transcription incident recovery."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

SCHEMA = "omo-explicit-production-approval/v1"
PROBLEM_ID = "4eb4189a2f72f0e5"
TRUSTED_APPROVAL_SHA256 = ""
FIELDS = {
    "schema",
    "watcher_problem",
    "approved_packet_sha256",
    "approved_actions",
    "root",
    "task_sha256",
    "todo_sha256",
    "protected_owner_sha256",
    "incident_receipt_path",
    "incident_receipt_sha256",
    "recovery_receipt_path",
    "recovery_receipt_sha256",
    "approval_scope",
}


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_approval(path: Path, expected_sha256: str) -> bytes:
    if not TRUSTED_APPROVAL_SHA256 or expected_sha256 != TRUSTED_APPROVAL_SHA256:
        raise OSError("no authenticated production approval is bound in this reviewed implementation")
    if not path.is_absolute() or path.resolve() != path:
        raise OSError("production approval must be an absolute canonical path")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o400:
            raise OSError("production approval must be an owner-read-only regular file")
        payload = b""
        while chunk := os.read(descriptor, min(65_536, 65_537 - len(payload))):
            payload += chunk
            if len(payload) > 65_536:
                break
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    bound = path.lstat()
    if len(payload) > 65_536:
        raise OSError("production approval is oversized")
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
        bound.st_dev,
        bound.st_ino,
    ) != (after.st_dev, after.st_ino):
        raise OSError("production approval changed while read")
    if sha256(payload) != expected_sha256:
        raise OSError("production approval bytes do not match the bound SHA-256")
    return payload


# 🧑 Human source `manager_mail/85c5dff58359-1270.txt:1-7`: "Find me a good transcription software ... supporting Mac OS and Linux."
def validate_approval(
    payload: bytes,
    *,
    approved_packet_sha256: str,
    root: Path,
    task_sha256: str,
    todo_sha256: str,
    protected_owner_sha256: str,
    incident_receipt_path: Path,
    incident_receipt_sha256: str,
    recovery_receipt_path: Path,
    recovery_receipt_sha256: str,
) -> None:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate production approval field: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(payload, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise OSError("production approval is not unambiguous UTF-8 JSON") from exc
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise OSError("production approval has an incomplete or unknown schema")
    if (
        value["schema"] != SCHEMA
        or value["watcher_problem"] != PROBLEM_ID
        or value["approved_packet_sha256"] != approved_packet_sha256
        or value["approved_actions"] != ["create-recovery-evidence", "close-shared-task"]
        or value["root"] != str(root)
        or value["task_sha256"] != task_sha256
        or value["todo_sha256"] != todo_sha256
        or value["protected_owner_sha256"] != protected_owner_sha256
        or value["incident_receipt_path"] != str(incident_receipt_path)
        or value["incident_receipt_sha256"] != incident_receipt_sha256
        or value["recovery_receipt_path"] != str(recovery_receipt_path)
        or value["recovery_receipt_sha256"] != recovery_receipt_sha256
        or value["approval_scope"] != "one-recovery-and-one-closure"
    ):
        raise OSError("production approval does not authorize this exact incident recovery")
    scalar_fields = FIELDS - {"approved_actions"}
    if any(not isinstance(value[field], str) or not value[field] for field in scalar_fields):
        raise OSError("production approval scalar bindings are incomplete")
