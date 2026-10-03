"""Offline-only drafts for a manager-approved, unaccepted-overlay GO."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


HASH = re.compile(r"[0-9a-f]{64}\Z")
KEY = re.compile(r"[0-9a-f]{64}\Z")
OPERATION = "unaccepted-overlay-only"
DOMAIN = b"cc-unaccepted-overlay-manager-go/v1\0"
REQUEST_FIELDS = frozenset({"schema_version", "operation", "nonce", "destination", "parent", "code", "inputs", "outputs", "policy"})
PARENT_FIELDS = frozenset({"path", "device", "inode", "uid", "gid", "mode_octal"})
INPUT_FIELDS = frozenset({"plan_sha256", "manifest_sha256", "contract_sha256", "ledger_sha256", "attestation_sha256", "scorer_verdict_sha256", "ordered_results_sha256"})
OUTPUT_NAMES = ("source2128-ten-scored-unaccepted.preview.json", "source2128-ten-scored-associations.preview.json")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _required_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or HASH.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _parse_request(request_bytes: bytes) -> dict[str, Any]:
    try:
        request = json.loads(request_bytes)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("request JSON is invalid") from error
    if not isinstance(request, dict) or _canonical(request) != request_bytes:
        raise ValueError("request must be canonical JSON with one newline")
    if (
        set(request) != REQUEST_FIELDS
        or type(request.get("schema_version")) is not int
        or request["schema_version"] != 1
        or request.get("operation") != OPERATION
        or not isinstance(request.get("destination"), str)
        or not request["destination"].startswith("/")
        or not isinstance(request.get("parent"), dict)
        or not isinstance(request.get("outputs"), list)
        or len(request["outputs"]) != len(OUTPUT_NAMES)
    ):
        raise ValueError("request is not an exact unaccepted-overlay request")
    _required_hash(request.get("nonce"), "nonce")
    destination = request["destination"]
    if posixpath.normpath(destination) != destination or posixpath.basename(destination) != "ten-scored-unaccepted-overlay":
        raise ValueError("request destination is not canonical")
    if not destination.startswith("/") or destination.startswith("//"):
        raise ValueError("request destination must have one leading slash")
    parent = request["parent"]
    if (
        set(parent) != PARENT_FIELDS
        or not isinstance(parent["path"], str)
        or posixpath.normpath(parent["path"]) != parent["path"]
        or parent["path"] != posixpath.dirname(destination)
        or any(type(parent[name]) is not int or parent[name] < 0 for name in ("device", "inode", "uid", "gid"))
        or not isinstance(parent["mode_octal"], str)
        or not re.fullmatch(r"[0-7]{3,4}", parent["mode_octal"])
    ):
        raise ValueError("request parent identity is invalid")
    if not isinstance(request["inputs"], dict) or set(request["inputs"]) != INPUT_FIELDS:
        raise ValueError("request evidence inventory is invalid")
    for name, digest in request["inputs"].items():
        _required_hash(digest, name)
    code = request["code"]
    if not isinstance(code, dict) or set(code) != {"commit", "source_sha256", "test_sha256"} or not isinstance(code["commit"], str) or re.fullmatch(r"[0-9a-f]{40}", code["commit"]) is None:
        raise ValueError("request code identity is invalid")
    _required_hash(code["source_sha256"], "source digest")
    _required_hash(code["test_sha256"], "test digest")
    if not all(isinstance(output, dict) for output in request["outputs"]):
        raise ValueError("request outputs must be objects")
    for output, expected_name in zip(request["outputs"], OUTPUT_NAMES, strict=True):
        if set(output) != {"name", "size_bytes", "sha256"} or output.get("name") != expected_name or type(output.get("size_bytes")) is not int or output["size_bytes"] <= 0:
            raise ValueError("request output name or size is invalid")
        _required_hash(output.get("sha256"), "output digest")
    if request.get("policy") != {
        "scored": True,
        "accepted": False,
        "production_supersession_permitted": False,
        "production_acceptance_permitted": False,
        "loader_writes_permitted": False,
        "database_writes_permitted": False,
        "provider_calls_permitted": False,
        "scorer_calls_permitted": False,
    }:
        raise ValueError("request changes unaccepted-only policy")
    return request


def inspect_private_key_custody(directory: Path, basename: str, *, expected_uid: int) -> None:
    """Read only: reject unsafe proposed custody without opening private key bytes."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.key", basename):
        raise ValueError("private key basename is invalid")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        directory_stat = os.fstat(directory_fd)
        if directory_stat.st_uid != expected_uid or stat.S_IMODE(directory_stat.st_mode) != 0o700:
            raise ValueError("private key directory must be owned and 0700")
        key_fd = os.open(basename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            key_stat = os.fstat(key_fd)
            if (
                not stat.S_ISREG(key_stat.st_mode)
                or key_stat.st_uid != expected_uid
                or key_stat.st_dev != directory_stat.st_dev
                or key_stat.st_nlink != 1
                or stat.S_IMODE(key_stat.st_mode) != 0o600
                or key_stat.st_size == 0
            ):
                raise ValueError("private key file must be owner-only, nonempty, and 0600")
        finally:
            os.close(key_fd)
    finally:
        os.close(directory_fd)


def public_pin_draft(
    public_key_hex: str,
    *,
    manager_approval_receipt_sha256: str,
    reviewed_code_commit: str,
    reviewed_source_sha256: str,
    reviewed_test_sha256: str,
) -> bytes:
    """Describe a proposed key pin; an external manager must authenticate its receipt."""
    if not isinstance(public_key_hex, str) or KEY.fullmatch(public_key_hex) is None:
        raise ValueError("Ed25519 public key must have 32 lowercase hex bytes")
    _required_hash(manager_approval_receipt_sha256, "manager approval receipt")
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed_code_commit):
        raise ValueError("reviewed commit is invalid")
    _required_hash(reviewed_source_sha256, "source digest")
    _required_hash(reviewed_test_sha256, "test digest")
    return _canonical(
        {
            "schema_version": 1,
            "operation": OPERATION,
            "manager_approval_receipt_sha256": manager_approval_receipt_sha256,
            "public_key_hex": public_key_hex,
            "public_key_sha256": _hash(bytes.fromhex(public_key_hex)),
            "reviewed_code_commit": reviewed_code_commit,
            "reviewed_source_sha256": reviewed_source_sha256,
            "reviewed_test_sha256": reviewed_test_sha256,
            "authority": "draft-only-not-authenticated",
        }
    )


def unsigned_go_draft(
    request_bytes: bytes,
    *,
    issued_at: datetime,
    expires_at: datetime,
    reviewed_commit: str,
    reviewed_source_sha256: str,
    reviewed_test_sha256: str,
    reviewed_request_sha256: str,
) -> bytes:
    """Build signable GO bytes without signing, storing, or granting authority."""
    request = _parse_request(request_bytes)
    _required_hash(reviewed_request_sha256, "reviewed request")
    if _hash(request_bytes) != reviewed_request_sha256:
        raise ValueError("request differs from independent review")
    if (
        issued_at.tzinfo is None
        or expires_at.tzinfo is None
        or issued_at.utcoffset() != timedelta(0)
        or expires_at.utcoffset() != timedelta(0)
        or not issued_at < expires_at <= issued_at + timedelta(minutes=30)
    ):
        raise ValueError("GO times must be UTC and within 30 minutes")
    if request.get("code") != {
        "commit": reviewed_commit,
        "source_sha256": reviewed_source_sha256,
        "test_sha256": reviewed_test_sha256,
    }:
        raise ValueError("GO request code differs from the reviewed bytes")
    payload = {
        "schema_version": 1,
        "operation": OPERATION,
        "nonce": request["nonce"],
        "request_sha256": _hash(request_bytes),
        "destination": request["destination"],
        "parent": request["parent"],
        "outputs": request["outputs"],
        "issued_at_utc": issued_at.astimezone(timezone.utc).isoformat(),
        "expires_at_utc": expires_at.astimezone(timezone.utc).isoformat(),
    }
    return DOMAIN + _canonical(payload)
