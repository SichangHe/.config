#!/usr/bin/env python3
"""Validate and retain guest email images outside Git repositories."""

from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

SCHEMA = "omo-guest-image-batch/v1"
CLEANUP_SCHEMA = "omo-guest-image-quarantine/v1"
AUTHENTICATION = "exact-visible-sender-and-gmail-transport-spf/v1"
GUEST_HEES_ADDRESS = "46496337@qq.com"
GUEST_HEES_MANAGER_TARGET = "guest_hees:0"
REFERENCE_RE = re.compile(r"guest-image:v1:([0-9a-f]{64})\Z")
SOURCE_ID_RE = re.compile(r"[A-Za-z0-9_.:@-]{1,255}\Z")
BATCH_OBJECT_RE = re.compile(r"batches/[0-9a-f]{64}\.json\Z")
IMAGE_OBJECT_RE = re.compile(r"objects/[0-9a-f]{64}\.(?:gif|jpg|png|webp)\Z")
PENDING_FILE_RE = re.compile(r"\.omo-guest-[0-9a-f]{16}\.pending\Z")
QUARANTINE_ID_RE = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_STORED_IMAGES = 100
MAX_STORED_BYTES = 100 * 1024 * 1024
MAX_STORED_BATCHES = 1_000
MAX_QUARANTINES = 100
SUPPORTED_SERVICES = frozenset({"chatgpt", "gemini"})
MIME_SUFFIXES = {
    "image/gif": frozenset({".gif"}),
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
    "image/webp": frozenset({".webp"}),
}
CANONICAL_SUFFIX = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class GuestImageError(ValueError):
    """A guest image or storage path failed a required safety check."""


@dataclass(frozen=True)
class IncomingImage:
    filename: str
    mime_type: str
    data: bytes
    sha256: str
    suffix: str

    @property
    def reference(self) -> str:
        return f"guest-image:v1:{self.sha256}"


@dataclass(frozen=True)
class ValidatedImage:
    reference: str
    path: Path
    filename: str
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class ServiceResolution:
    schema: str
    service: str
    reference: str
    path: Path
    sha256: str
    mime_type: str
    size_bytes: int
    batch_receipt: Path
    batch_receipt_sha256: str
    sender: str
    route_target: str
    authentication: str


def default_root() -> Path:
    configured = os.environ.get("OMO_GUEST_IMAGE_ROOT", "")
    if configured:
        return Path(configured)
    manager_state = os.environ.get("OMO_MANAGER_STATE_DIR", "")
    if manager_state:
        return Path(manager_state) / "guest-images"
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return state_home / "omo-manager" / "guest-images"


def _existing_chain(path: Path) -> list[Path]:
    return [candidate for candidate in (path, *path.parents) if candidate.exists() or candidate.is_symlink()]


def _validate_no_git_or_symlink(path: Path) -> None:
    if not path.is_absolute():
        raise GuestImageError("guest image root must be absolute")
    for candidate in _existing_chain(path):
        mode = os.lstat(candidate).st_mode
        if stat.S_ISLNK(mode):
            raise GuestImageError(f"guest image path must not traverse symlinks: {candidate}")
        git_marker = candidate / ".git"
        if git_marker.exists() or git_marker.is_symlink():
            raise GuestImageError("guest images must be stored outside every Git repository")
        if (candidate / "HEAD").is_file() and (candidate / "objects").is_dir() and (candidate / "refs").is_dir():
            raise GuestImageError("guest images must be stored outside every Git repository, including bare repositories")


def _validate_private_dir(path: Path) -> None:
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise GuestImageError(f"guest image directory must be owner-private: {path}")


def prepare_root(root: Path | None = None) -> Path:
    selected = root or default_root()
    _validate_no_git_or_symlink(selected)
    if selected.exists():
        _validate_private_dir(selected)
    else:
        missing = [candidate for candidate in (selected, *selected.parents) if not candidate.exists()]
        for candidate in reversed(missing):
            candidate.mkdir(mode=0o700)
            _validate_private_dir(candidate)
            _fsync_dir(candidate)
            _fsync_dir(candidate.parent)
    _validate_no_git_or_symlink(selected)
    _validate_private_dir(selected)
    _fsync_dir(selected)
    _fsync_dir(selected.parent)
    for name in ("objects", "batches", "quarantine"):
        child = selected / name
        if child.is_symlink():
            raise GuestImageError(f"guest image storage entry must not be a symlink: {child}")
        if child.exists():
            _validate_private_dir(child)
        else:
            child.mkdir(mode=0o700)
        _validate_private_dir(child)
        _fsync_dir(child)
        _fsync_dir(selected)
    return selected


def _sniff_mime(data: bytes) -> str | None:
    if len(data) >= 20 and data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and data[-12:-8] == b"\x00\x00\x00\x00" and data[-8:-4] == b"IEND":
        return "image/png"
    if len(data) >= 4 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
        return "image/jpeg"
    if len(data) >= 14 and data[:6] in {b"GIF87a", b"GIF89a"} and data.endswith(b";"):
        return "image/gif"
    if len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}:
        declared_size = int.from_bytes(data[4:8], "little") + 8
        if declared_size == len(data):
            return "image/webp"
    return None


def _validate_filename(filename: str) -> str:
    if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise GuestImageError("image filename must be a plain basename")
    if len(filename.encode("utf-8")) > 255 or any(ord(character) < 32 or ord(character) == 127 for character in filename):
        raise GuestImageError("image filename is invalid")
    return Path(filename).suffix.casefold()


def incoming_images(message: Message) -> tuple[IncomingImage, ...]:
    images: list[IncomingImage] = []
    total_bytes = 0
    for part in message.walk():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if part.is_multipart():
            if filename is not None or disposition == "attachment":
                raise GuestImageError("multipart MIME attachments are not supported")
            continue
        is_attachment = filename is not None or part.get_content_maintype() == "image" or disposition == "attachment"
        if not is_attachment:
            continue
        if len(images) >= MAX_IMAGES_PER_MESSAGE:
            raise GuestImageError(f"guest mail may contain at most {MAX_IMAGES_PER_MESSAGE} images")
        mime_type = part.get_content_type().casefold()
        if mime_type not in MIME_SUFFIXES:
            raise GuestImageError(f"unsupported attachment MIME type: {mime_type}")
        if filename is None:
            raise GuestImageError("each image attachment must have a filename")
        suffix = _validate_filename(filename)
        if suffix not in MIME_SUFFIXES[mime_type]:
            raise GuestImageError("image filename extension does not match its declared MIME type")
        transfer_encodings = [str(value).casefold() for value in part.get_all("Content-Transfer-Encoding", [])]
        raw_payload = part.get_payload(decode=False)
        if transfer_encodings != ["base64"] or not isinstance(raw_payload, str):
            raise GuestImageError("image attachments require one bounded base64 transfer encoding")
        max_base64_chars = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
        max_encoded_chars = max_base64_chars + 2 * (max_base64_chars // 76) + 4
        if len(raw_payload) > max_encoded_chars:
            raise GuestImageError("encoded image attachment exceeds the bounded decode limit")
        compact_payload = "".join(raw_payload.split())
        if len(compact_payload) > max_base64_chars:
            raise GuestImageError("encoded image attachment exceeds the bounded decode limit")
        try:
            payload = base64.b64decode(compact_payload, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GuestImageError("image attachment base64 is invalid") from exc
        if not payload:
            raise GuestImageError("image attachment payload is empty")
        if len(payload) > MAX_IMAGE_BYTES:
            raise GuestImageError(f"each guest image must be at most {MAX_IMAGE_BYTES} bytes")
        total_bytes += len(payload)
        if total_bytes > MAX_MESSAGE_IMAGE_BYTES:
            raise GuestImageError(f"guest images in one message must total at most {MAX_MESSAGE_IMAGE_BYTES} bytes")
        sniffed = _sniff_mime(payload)
        if sniffed != mime_type:
            raise GuestImageError("image content does not match its declared MIME type")
        digest = hashlib.sha256(payload).hexdigest()
        images.append(IncomingImage(filename, mime_type, payload, digest, CANONICAL_SUFFIX[mime_type]))
    return tuple(images)


def _regular_private_file(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise GuestImageError(f"guest image file must be an owner-private regular file: {path}")
    return info


def _write_new_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _pending_path(path: Path) -> Path:
    return path.parent / f".omo-guest-{secrets.token_hex(8)}.pending"


def _publish_new_file(path: Path, payload: bytes) -> None:
    pending = _pending_path(path)
    try:
        _write_new_file(pending, payload)
        os.link(pending, path, follow_symlinks=False)
        _fsync_dir(path.parent)
    finally:
        pending.unlink(missing_ok=True)
        _fsync_dir(path.parent)


def _replace_file(path: Path, payload: bytes) -> None:
    pending = _pending_path(path)
    try:
        _write_new_file(pending, payload)
        os.replace(pending, path)
        _fsync_dir(path.parent)
    finally:
        pending.unlink(missing_ok=True)


def _replace_bounded_file(root: Path, path: Path, payload: bytes) -> None:
    stored = _batch_files(root, quarantined=False) + _batch_files(root, quarantined=True) + _object_files(root, quarantined=False) + _object_files(root, quarantined=True) + sorted((root / "quarantine").glob("*/receipt.json"))
    current_bytes = sum(_regular_private_file(stored_path).st_size for stored_path in stored)
    old_bytes = _regular_private_file(path).st_size
    if current_bytes - old_bytes + len(payload) > MAX_STORED_BYTES:
        raise GuestImageError("guest image store lacks bounded capacity for a transaction receipt")
    _replace_file(path, payload)


def _ensure_replacement_capacity(root: Path, path: Path, payloads: tuple[bytes, ...]) -> None:
    stored = _batch_files(root, quarantined=False) + _batch_files(root, quarantined=True) + _object_files(root, quarantined=False) + _object_files(root, quarantined=True) + sorted((root / "quarantine").glob("*/receipt.json"))
    current_bytes = sum(_regular_private_file(stored_path).st_size for stored_path in stored)
    old_bytes = _regular_private_file(path).st_size
    if current_bytes - old_bytes + max(map(len, payloads)) > MAX_STORED_BYTES:
        raise GuestImageError("guest image store lacks bounded capacity for a transaction receipt")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_move(source: Path, destination: Path) -> None:
    _regular_private_file(source)
    if destination.exists() or destination.is_symlink():
        raise GuestImageError(f"guest image move destination already exists: {destination}")
    os.replace(source, destination)
    _fsync_dir(source.parent)
    if destination.parent != source.parent:
        _fsync_dir(destination.parent)


def _file_digest(path: Path) -> tuple[int, str]:
    info = _regular_private_file(path)
    payload = path.read_bytes()
    if len(payload) != info.st_size:
        raise GuestImageError("guest image file changed while calculating evidence")
    return len(payload), hashlib.sha256(payload).hexdigest()


@contextmanager
def _storage_lock(root: Path):
    lock_path = root / ".lock"
    if lock_path.is_symlink():
        raise GuestImageError("guest image store lock must not be a symlink")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _regular_private_file(lock_path)
        with os.fdopen(lock_fd, "rb", closefd=False) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        os.close(lock_fd)


def _read_json(path: Path) -> dict[str, object]:
    _regular_private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GuestImageError(f"invalid guest image metadata: {path}") from exc
    if not isinstance(value, dict):
        raise GuestImageError(f"invalid guest image metadata: {path}")
    return value


def _batch_files(root: Path, *, quarantined: bool) -> list[Path]:
    if not quarantined:
        return sorted((root / "batches").glob("*.json"))
    return sorted((root / "quarantine").glob("*/batches/*.json"))


def _object_files(root: Path, *, quarantined: bool) -> list[Path]:
    if not quarantined:
        return sorted((root / "objects").glob("*"))
    return sorted((root / "quarantine").glob("*/objects/*"))


def _remove_pending_files(root: Path) -> None:
    directories = [root / "objects", root / "batches"]
    for transaction in (root / "quarantine").iterdir():
        if transaction.is_symlink() or not transaction.is_dir() or QUARANTINE_ID_RE.fullmatch(transaction.name) is None:
            raise GuestImageError("guest image quarantine contains an invalid transaction path")
        _validate_private_dir(transaction)
        directories.append(transaction)
        directories.extend(path for path in (transaction / "objects", transaction / "batches") if path.is_dir() and not path.is_symlink())
    for directory in directories:
        removed = False
        for path in directory.iterdir():
            if not path.name.startswith(".omo-guest-"):
                continue
            if PENDING_FILE_RE.fullmatch(path.name) is None:
                raise GuestImageError("guest image store contains an invalid pending file")
            _regular_private_file(path)
            path.unlink()
            removed = True
        if removed:
            _fsync_dir(directory)


def _remove_empty_incomplete_quarantine(root: Path, transaction: Path) -> bool:
    if (transaction / "receipt.json").exists() or (transaction / "receipt.json").is_symlink():
        return False
    children = list(transaction.iterdir())
    if any(child.name not in {"objects", "batches"} or child.is_symlink() or not child.is_dir() or any(child.iterdir()) for child in children):
        raise GuestImageError("incomplete guest image quarantine is not empty")
    for child in children:
        child.rmdir()
    transaction.rmdir()
    _fsync_dir(root / "quarantine")
    return True


def _validated_move_entries(transaction: Path, value: dict[str, object]) -> list[dict[str, object]]:
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise GuestImageError("quarantine receipt lacks move entries")
    entries: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != {"kind", "from", "to", "size_bytes", "sha256"}:
            raise GuestImageError("quarantine receipt contains an invalid move entry")
        active_relative = raw_entry.get("from")
        quarantined_relative = raw_entry.get("to")
        kind = raw_entry.get("kind")
        size_bytes = raw_entry.get("size_bytes")
        sha256 = raw_entry.get("sha256")
        valid_active = BATCH_OBJECT_RE.fullmatch(active_relative) if kind == "batch" and isinstance(active_relative, str) else IMAGE_OBJECT_RE.fullmatch(active_relative) if kind == "object" and isinstance(active_relative, str) else None
        if (
            valid_active is None
            or not isinstance(quarantined_relative, str)
            or quarantined_relative != f"quarantine/{transaction.name}/{active_relative}"
            or type(size_bytes) is not int
            or size_bytes < 1
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or active_relative in seen_paths
        ):
            raise GuestImageError("quarantine receipt contains an unsafe move entry")
        assert isinstance(active_relative, str)
        seen_paths.add(active_relative)
        entries.append(raw_entry)
    return entries


def _validated_quarantine_receipt(transaction: Path, value: dict[str, object]) -> tuple[str, list[dict[str, object]]]:
    state = value.get("state")
    base_keys = {"schema", "state", "created_at", "older_than_days", "entries"}
    expected_keys = base_keys
    if state == "restoring":
        expected_keys = base_keys | {"restore_started_at"}
    elif state == "restored":
        expected_keys = base_keys | {"restore_started_at", "restored_at", "retained_quarantined_duplicates"}
    if set(value) != expected_keys or value.get("schema") != CLEANUP_SCHEMA or state not in {"planned", "quarantined", "restoring", "restored"}:
        raise GuestImageError("guest image quarantine receipt schema or state is invalid")
    if QUARANTINE_ID_RE.fullmatch(transaction.name) is None:
        raise GuestImageError("guest image quarantine transaction id is invalid")
    _ = _utc_timestamp(value["created_at"], "quarantine created_at")
    older_than_days = value.get("older_than_days")
    if type(older_than_days) is not int or older_than_days < 1:
        raise GuestImageError("quarantine older_than_days must be a positive exact integer")
    entries = _validated_move_entries(transaction, value)
    if state in {"restoring", "restored"}:
        _ = _utc_timestamp(value["restore_started_at"], "quarantine restore_started_at")
    if state == "restored":
        _ = _utc_timestamp(value["restored_at"], "quarantine restored_at")
        raw_retained = value.get("retained_quarantined_duplicates")
        valid_paths = {str(entry["to"]) for entry in entries}
        if not isinstance(raw_retained, list) or any(not isinstance(path, str) or path not in valid_paths for path in raw_retained) or len(raw_retained) != len(set(raw_retained)):
            raise GuestImageError("quarantine retained duplicate paths are invalid")
    assert isinstance(state, str)
    return state, entries


def _complete_moves(root: Path, entries: list[dict[str, object]], *, restoring: bool) -> list[str]:
    ordered = list(reversed(entries)) if restoring else entries
    retained_duplicates: list[str] = []
    for entry in ordered:
        active = root / str(entry["from"])
        quarantined = root / str(entry["to"])
        source, destination = (quarantined, active) if restoring else (active, quarantined)
        if source.exists() and not source.is_symlink() and not destination.exists() and not destination.is_symlink():
            size_bytes, sha256 = _file_digest(source)
            if entry["size_bytes"] != size_bytes or entry["sha256"] != sha256:
                raise GuestImageError("quarantine receipt evidence does not match its source file")
            _durable_move(source, destination)
            continue
        if destination.exists() and not destination.is_symlink() and not source.exists() and not source.is_symlink():
            size_bytes, sha256 = _file_digest(destination)
            if entry["size_bytes"] != size_bytes or entry["sha256"] != sha256:
                raise GuestImageError("quarantine receipt evidence does not match its moved file")
            continue
        if restoring and source.exists() and not source.is_symlink() and destination.exists() and not destination.is_symlink():
            source_size, source_sha256 = _file_digest(source)
            destination_size, destination_sha256 = _file_digest(destination)
            if entry["size_bytes"] != source_size or entry["sha256"] != source_sha256 or (source_size, source_sha256) != (destination_size, destination_sha256):
                raise GuestImageError("restore destination conflicts with quarantined evidence")
            retained_duplicates.append(str(entry["to"]))
            continue
        raise GuestImageError("quarantine recovery found an ambiguous move state")
    return retained_duplicates


def _recover_transactions(root: Path) -> None:
    _remove_pending_files(root)
    for transaction in sorted((root / "quarantine").iterdir()):
        if _remove_empty_incomplete_quarantine(root, transaction):
            continue
        receipt = transaction / "receipt.json"
        value = _read_json(receipt)
        state, entries = _validated_quarantine_receipt(transaction, value)
        if state == "planned":
            _ = _complete_moves(root, entries, restoring=False)
            value["state"] = "quarantined"
            _replace_bounded_file(root, receipt, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        elif state == "quarantined":
            _ = _complete_moves(root, entries, restoring=False)
        elif state == "restoring":
            retained_duplicates = _complete_moves(root, entries, restoring=True)
            value["state"] = "restored"
            value["restored_at"] = value.get("restore_started_at")
            value["retained_quarantined_duplicates"] = retained_duplicates
            _replace_bounded_file(root, receipt, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        else:
            retained_duplicates = _complete_moves(root, entries, restoring=True)
            if value["retained_quarantined_duplicates"] != retained_duplicates:
                raise GuestImageError("restored quarantine receipt does not match retained files")


def _validate_inventory(root: Path) -> tuple[list[Path], list[Path]]:
    allowed_root_entries = {".lock", "objects", "batches", "quarantine"}
    if any(entry.name not in allowed_root_entries for entry in root.iterdir()):
        raise GuestImageError("guest image store contains an unexpected root entry")
    active_batches = sorted((root / "batches").iterdir())
    active_objects = sorted((root / "objects").iterdir())
    if any(BATCH_OBJECT_RE.fullmatch(str(path.relative_to(root))) is None for path in active_batches):
        raise GuestImageError("guest image batch directory contains an unexpected entry")
    if any(IMAGE_OBJECT_RE.fullmatch(str(path.relative_to(root))) is None for path in active_objects):
        raise GuestImageError("guest image object directory contains an unexpected entry")
    for entry in (root / "quarantine").iterdir():
        if entry.is_symlink() or not entry.is_dir():
            raise GuestImageError("guest image quarantine contains an invalid entry")
        _validate_private_dir(entry)
        if any(child.name not in {"objects", "batches", "receipt.json"} for child in entry.iterdir()):
            raise GuestImageError("guest image quarantine contains an unexpected entry")
        for name in ("objects", "batches"):
            child = entry / name
            if not child.exists() or child.is_symlink():
                raise GuestImageError("guest image quarantine structure is incomplete")
            _validate_private_dir(child)
            expected_re = IMAGE_OBJECT_RE if name == "objects" else BATCH_OBJECT_RE
            if any(expected_re.fullmatch(f"{name}/{path.name}") is None for path in child.iterdir()):
                raise GuestImageError("guest image quarantine contains an invalid object")
        if not (entry / "receipt.json").exists():
            raise GuestImageError("guest image quarantine receipt is missing")
        receipt = _read_json(entry / "receipt.json")
        state, _entries = _validated_quarantine_receipt(entry, receipt)
        if state not in {"quarantined", "restored"}:
            raise GuestImageError("guest image quarantine receipt state is invalid")
    batches = _batch_files(root, quarantined=False) + _batch_files(root, quarantined=True)
    objects = _object_files(root, quarantined=False) + _object_files(root, quarantined=True)
    if len(batches) > MAX_STORED_BATCHES or len(objects) > MAX_STORED_IMAGES:
        raise GuestImageError("guest image store exceeds its configured count bound")
    total_bytes = 0
    quarantine_receipts = sorted((root / "quarantine").glob("*/receipt.json"))
    for batch in batches:
        _ = _metadata_references(_read_json(batch), batch, allowed_states=frozenset({"planned", "active"}))
    for path in batches + objects + quarantine_receipts:
        total_bytes += _regular_private_file(path).st_size
    if total_bytes > MAX_STORED_BYTES:
        raise GuestImageError("guest image store exceeds its configured byte bound")
    return batches, objects


def _batch_key(source_id: str) -> str:
    if SOURCE_ID_RE.fullmatch(source_id) is None:
        raise GuestImageError("guest image source identity is invalid")
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise GuestImageError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GuestImageError(f"{label} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise GuestImageError(f"{label} must be a canonical UTC timestamp")
    return parsed


def _metadata_references(
    metadata: dict[str, object],
    batch_path: Path,
    *,
    allowed_states: frozenset[str] = frozenset({"active"}),
) -> tuple[str, ...]:
    expected_keys = {"schema", "state", "source_id", "received_at", "sender", "route_target", "authentication", "images"}
    if (
        set(metadata) != expected_keys
        or metadata.get("schema") != SCHEMA
        or metadata.get("sender") != GUEST_HEES_ADDRESS
        or metadata.get("route_target") != GUEST_HEES_MANAGER_TARGET
        or metadata.get("authentication") != AUTHENTICATION
        or not isinstance(metadata.get("source_id"), str)
        or SOURCE_ID_RE.fullmatch(str(metadata["source_id"])) is None
        or metadata.get("state") not in allowed_states
    ):
        raise GuestImageError("guest image metadata trust fields are invalid")
    source_id = metadata["source_id"]
    assert isinstance(source_id, str)
    if batch_path.name != f"{_batch_key(source_id)}.json":
        raise GuestImageError("guest image metadata source identity does not match its receipt path")
    _ = _utc_timestamp(metadata["received_at"], "guest image received_at")
    raw_images = metadata.get("images")
    if not isinstance(raw_images, list) or not 1 <= len(raw_images) <= MAX_IMAGES_PER_MESSAGE:
        raise GuestImageError("guest image metadata must contain one to four images")
    references: list[str] = []
    total_bytes = 0
    required_fields = {"filename", "mime_type", "size_bytes", "sha256", "reference", "object"}
    for raw_image in raw_images:
        if not isinstance(raw_image, dict) or set(raw_image) != required_fields:
            raise GuestImageError("guest image metadata contains an invalid image entry")
        filename = raw_image.get("filename")
        mime_type = raw_image.get("mime_type")
        size_bytes = raw_image.get("size_bytes")
        sha256 = raw_image.get("sha256")
        reference = raw_image.get("reference")
        object_path = raw_image.get("object")
        if not isinstance(filename, str) or not isinstance(mime_type, str) or mime_type not in MIME_SUFFIXES:
            raise GuestImageError("guest image metadata contains an invalid filename or MIME type")
        suffix = _validate_filename(filename)
        if suffix not in MIME_SUFFIXES[mime_type]:
            raise GuestImageError("guest image metadata extension does not match its MIME type")
        if type(size_bytes) is not int or not 1 <= size_bytes <= MAX_IMAGE_BYTES:
            raise GuestImageError("guest image metadata contains an invalid size")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise GuestImageError("guest image metadata contains an invalid digest")
        if reference != f"guest-image:v1:{sha256}" or object_path != f"objects/{sha256}{CANONICAL_SUFFIX[mime_type]}":
            raise GuestImageError("guest image metadata contains an invalid reference or object path")
        assert isinstance(reference, str)
        total_bytes += size_bytes
        references.append(reference)
    if total_bytes > MAX_MESSAGE_IMAGE_BYTES:
        raise GuestImageError("guest image metadata exceeds the per-message byte bound")
    if len(references) != len(set(references)):
        raise GuestImageError("guest image metadata contains duplicate references")
    return tuple(references)


def store_message_images(
    message: Message,
    *,
    sender: str,
    route_target: str,
    authentication: str,
    source_id: str,
    root: Path | None = None,
    received_at: datetime | None = None,
) -> tuple[str, ...]:
    if sender != GUEST_HEES_ADDRESS:
        raise GuestImageError("guest images require the exact pinned sender")
    if route_target != GUEST_HEES_MANAGER_TARGET:
        raise GuestImageError("guest images require the dedicated guest manager route")
    if authentication != AUTHENTICATION:
        raise GuestImageError("guest images require the exact authenticated-intake marker")
    images = incoming_images(message)
    if not images:
        return ()
    if len({image.sha256 for image in images}) != len(images):
        raise GuestImageError("guest mail must not repeat an identical image attachment")
    selected = prepare_root(root)
    batch_path = selected / "batches" / f"{_batch_key(source_id)}.json"
    stamp = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    metadata_images = [
        {
            "filename": image.filename,
            "mime_type": image.mime_type,
            "size_bytes": len(image.data),
            "sha256": image.sha256,
            "reference": image.reference,
            "object": f"objects/{image.sha256}{image.suffix}",
        }
        for image in images
    ]
    metadata = {
        "schema": SCHEMA,
        "state": "planned",
        "source_id": source_id,
        "received_at": stamp,
        "sender": GUEST_HEES_ADDRESS,
        "route_target": GUEST_HEES_MANAGER_TARGET,
        "authentication": AUTHENTICATION,
        "images": metadata_images,
    }
    with _storage_lock(selected):
        _recover_transactions(selected)
        batches, objects = _validate_inventory(selected)
        expected_references = tuple(image.reference for image in images)
        batch_exists = batch_path.exists() or batch_path.is_symlink()
        if batch_exists:
            stored_metadata = _read_json(batch_path)
            if (
                stored_metadata.get("source_id") != source_id
                or stored_metadata.get("sender") != GUEST_HEES_ADDRESS
                or stored_metadata.get("route_target") != GUEST_HEES_MANAGER_TARGET
                or stored_metadata.get("authentication") != AUTHENTICATION
                or stored_metadata.get("images") != metadata_images
                or _metadata_references(stored_metadata, batch_path, allowed_states=frozenset({"planned", "active"}))
                != expected_references
            ):
                raise GuestImageError("existing guest image batch does not match this message")
            if stored_metadata["state"] == "active":
                for image in images:
                    _ = _read_validated_object(selected / "objects" / f"{image.sha256}{image.suffix}", image.sha256)
                return expected_references
            metadata = stored_metadata
        existing_names = {path.name for path in _object_files(selected, quarantined=False)}
        new_images = [image for image in images if f"{image.sha256}{image.suffix}" not in existing_names]
        new_batch_count = 0 if batch_exists else 1
        if len(objects) + len(new_images) > MAX_STORED_IMAGES or len(batches) + new_batch_count > MAX_STORED_BATCHES:
            raise GuestImageError("guest image store lacks bounded capacity for this message")
        receipts = sorted((selected / "quarantine").glob("*/receipt.json"))
        planned_payload = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8")
        projected_bytes = sum(_regular_private_file(path).st_size for path in objects + batches + receipts) + sum(len(image.data) for image in new_images) + (0 if batch_exists else len(planned_payload))
        if projected_bytes > MAX_STORED_BYTES:
            raise GuestImageError("guest image store lacks bounded byte capacity for this message")
        if not batch_exists:
            _publish_new_file(batch_path, planned_payload)
        for image in images:
            object_path = selected / "objects" / f"{image.sha256}{image.suffix}"
            if object_path.exists() or object_path.is_symlink():
                validated = _read_validated_object(object_path, image.sha256)
                if validated.mime_type != image.mime_type:
                    raise GuestImageError("stored guest image MIME type changed")
                continue
            _publish_new_file(object_path, image.data)
        _fsync_dir(selected / "objects")
        metadata["state"] = "active"
        _replace_bounded_file(selected, batch_path, (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode("utf-8"))
        return expected_references


def _read_validated_object(path: Path, expected_digest: str) -> ValidatedImage:
    _regular_private_file(path)
    if path.is_symlink() or path.name != f"{expected_digest}{path.suffix}":
        raise GuestImageError("guest image object path is invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise GuestImageError("guest image object changed during validation")
        if info.st_size > MAX_IMAGE_BYTES:
            raise GuestImageError("stored guest image exceeds the attachment size bound")
        data = b""
        while chunk := os.read(fd, min(1024 * 1024, MAX_IMAGE_BYTES + 1 - len(data))):
            data += chunk
            if len(data) > MAX_IMAGE_BYTES:
                raise GuestImageError("stored guest image exceeds the attachment size bound")
    finally:
        os.close(fd)
    digest = hashlib.sha256(data).hexdigest()
    mime_type = _sniff_mime(data)
    if digest != expected_digest or mime_type is None or path.suffix not in MIME_SUFFIXES[mime_type]:
        raise GuestImageError("stored guest image failed digest, MIME, or extension validation")
    return ValidatedImage(f"guest-image:v1:{digest}", path, path.name, mime_type, data)


def _resolve_reference_unlocked(reference: str, selected: Path) -> ValidatedImage:
    match = REFERENCE_RE.fullmatch(reference)
    if match is None:
        raise GuestImageError("guest image reference is invalid")
    digest = match.group(1)
    candidates = [path for path in (selected / "objects").glob(f"{digest}.*") if path.exists() or path.is_symlink()]
    if len(candidates) != 1:
        raise GuestImageError("guest image reference does not resolve to exactly one active object")
    bindings: list[dict[str, object]] = []
    for batch_path in _batch_files(selected, quarantined=False):
        metadata = _read_json(batch_path)
        references = _metadata_references(metadata, batch_path, allowed_states=frozenset({"planned", "active"}))
        if metadata["state"] != "active" or reference not in references:
            continue
        raw_images = metadata["images"]
        assert isinstance(raw_images, list)
        bindings.extend(entry for entry in raw_images if isinstance(entry, dict) and entry.get("reference") == reference)
    if not bindings:
        raise GuestImageError("guest image reference is not bound to an active intake batch")
    image = _read_validated_object(candidates[0], digest)
    for binding in bindings:
        if (
            binding.get("sha256") != digest
            or binding.get("mime_type") != image.mime_type
            or binding.get("size_bytes") != len(image.data)
            or binding.get("object") != f"objects/{image.path.name}"
            or not isinstance(binding.get("filename"), str)
        ):
            raise GuestImageError("guest image intake receipt does not match the active object")
    return image


def resolve_reference(reference: str, root: Path | None = None) -> ValidatedImage:
    selected = prepare_root(root)
    with _storage_lock(selected):
        _recover_transactions(selected)
        _validate_inventory(selected)
        return _resolve_reference_unlocked(reference, selected)


def reply_attachments(references: tuple[str, ...], *, recipient: str, root: Path | None = None) -> tuple[ValidatedImage, ...]:
    if recipient != GUEST_HEES_ADDRESS:
        raise GuestImageError("guest image replies require the exact pinned recipient")
    if not references:
        raise GuestImageError("guest image replies require one to four unique references")
    if len(references) > MAX_IMAGES_PER_MESSAGE or len(references) != len(set(references)):
        raise GuestImageError("guest image replies require one to four unique references")
    selected = prepare_root(root)
    with _storage_lock(selected):
        _recover_transactions(selected)
        _validate_inventory(selected)
        resolved = tuple(_resolve_reference_unlocked(reference, selected) for reference in references)
        if sum(len(image.data) for image in resolved) > MAX_MESSAGE_IMAGE_BYTES:
            raise GuestImageError("guest reply images exceed the total attachment size bound")
        return resolved


def resolve_for_service(reference: str, *, service: str, root: Path | None = None) -> ServiceResolution:
    if service not in SUPPORTED_SERVICES:
        raise GuestImageError(f"unsupported guest image service: {service}")
    selected = prepare_root(root)
    with _storage_lock(selected):
        _recover_transactions(selected)
        _validate_inventory(selected)
        image = _resolve_reference_unlocked(reference, selected)
        receipts = [
            batch
            for batch in _batch_files(selected, quarantined=False)
            if reference in _metadata_references(_read_json(batch), batch)
        ]
        if not receipts:
            raise GuestImageError("guest image reference has no active intake receipt")
        receipt = receipts[0]
        metadata = _read_json(receipt)
        if (
            metadata.get("sender") != GUEST_HEES_ADDRESS
            or metadata.get("route_target") != GUEST_HEES_MANAGER_TARGET
            or metadata.get("authentication") != AUTHENTICATION
        ):
            raise GuestImageError("guest image intake receipt trust fields are invalid")
        raw_images = metadata.get("images")
        assert isinstance(raw_images, list)
        entries = [entry for entry in raw_images if isinstance(entry, dict) and entry.get("reference") == reference]
        if len(entries) != 1:
            raise GuestImageError("guest image intake receipt does not bind one exact image")
        entry = entries[0]
        expected_object = f"objects/{image.path.name}"
        if (
            entry.get("sha256") != image.reference.rpartition(":")[2]
            or entry.get("mime_type") != image.mime_type
            or entry.get("size_bytes") != len(image.data)
            or entry.get("object") != expected_object
            or not isinstance(entry.get("filename"), str)
        ):
            raise GuestImageError("guest image intake receipt does not match the active object")
        receipt_payload = receipt.read_bytes()
        return ServiceResolution(
            schema="omo-guest-image-resolution/v1",
            service=service,
            reference=reference,
            path=image.path,
            sha256=reference.rpartition(":")[2],
            mime_type=image.mime_type,
            size_bytes=len(image.data),
            batch_receipt=receipt,
            batch_receipt_sha256=hashlib.sha256(receipt_payload).hexdigest(),
            sender=str(metadata["sender"]),
            route_target=str(metadata["route_target"]),
            authentication=str(metadata["authentication"]),
        )


def _quarantine_expired_locked(selected: Path, *, older_than_days: int, current: datetime) -> Path | None:
    cutoff = current - timedelta(days=older_than_days)
    active_batches = _batch_files(selected, quarantined=False)
    selected_batches: list[tuple[Path, dict[str, object]]] = []
    retained_references: set[str] = set()
    selected_active_references: set[str] = set()
    for batch_path in active_batches:
        metadata = _read_json(batch_path)
        references = _metadata_references(metadata, batch_path, allowed_states=frozenset({"planned", "active"}))
        raw_stamp = metadata.get("received_at")
        if not isinstance(raw_stamp, str):
            raise GuestImageError("guest image metadata lacks a received timestamp")
        try:
            received_at = datetime.fromisoformat(raw_stamp)
        except ValueError as exc:
            raise GuestImageError("guest image metadata has an invalid received timestamp") from exc
        if received_at.tzinfo is None:
            raise GuestImageError("guest image metadata timestamp lacks a timezone")
        if received_at < cutoff:
            selected_batches.append((batch_path, metadata))
            if metadata["state"] == "active":
                selected_active_references.update(references)
        else:
            retained_references.update(references)
    if not selected_batches:
        return None
    quarantine_root = selected / "quarantine"
    existing_quarantines = [path for path in quarantine_root.iterdir() if path.is_dir()]
    if len(existing_quarantines) >= MAX_QUARANTINES:
        raise GuestImageError("guest image quarantine count bound is full")
    quarantine_id = f"{current.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    destination_root = quarantine_root / quarantine_id
    destination_batches = destination_root / "batches"
    destination_objects = destination_root / "objects"
    references = {
        reference
        for batch_path, metadata in selected_batches
        for reference in _metadata_references(metadata, batch_path, allowed_states=frozenset({"planned", "active"}))
    }
    object_paths: list[Path] = []
    for reference in sorted(references - retained_references):
        digest = reference.rpartition(":")[2]
        candidates = list((selected / "objects").glob(f"{digest}.*"))
        if not candidates and reference not in selected_active_references:
            continue
        if len(candidates) != 1:
            raise GuestImageError("expired guest image reference has an invalid object set")
        object_paths.append(_read_validated_object(candidates[0], digest).path)
    entries: list[dict[str, object]] = []
    for kind, paths, destination in (
        ("batch", [path for path, _metadata in selected_batches], destination_batches),
        ("object", object_paths, destination_objects),
    ):
        for path in paths:
            size_bytes, sha256 = _file_digest(path)
            entries.append(
                {
                    "kind": kind,
                    "from": str(path.relative_to(selected)),
                    "to": str((destination / path.name).relative_to(selected)),
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                }
            )
    receipt = destination_root / "receipt.json"
    receipt_value: dict[str, object] = {
        "schema": CLEANUP_SCHEMA,
        "state": "planned",
        "created_at": current.isoformat(),
        "older_than_days": older_than_days,
        "entries": entries,
    }
    planned_payload = (json.dumps(receipt_value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    receipt_value["state"] = "quarantined"
    quarantined_payload = (json.dumps(receipt_value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    existing_paths = _batch_files(selected, quarantined=False) + _batch_files(selected, quarantined=True) + _object_files(selected, quarantined=False) + _object_files(selected, quarantined=True) + sorted(quarantine_root.glob("*/receipt.json"))
    existing_bytes = sum(_regular_private_file(path).st_size for path in existing_paths)
    if existing_bytes + max(len(planned_payload), len(quarantined_payload)) > MAX_STORED_BYTES:
        raise GuestImageError("guest image store lacks bounded receipt capacity for cleanup")
    destination_root.mkdir(mode=0o700)
    _fsync_dir(quarantine_root)
    destination_batches.mkdir(mode=0o700)
    _fsync_dir(destination_batches)
    _fsync_dir(destination_root)
    destination_objects.mkdir(mode=0o700)
    _fsync_dir(destination_objects)
    _fsync_dir(destination_root)
    receipt_value["state"] = "planned"
    _publish_new_file(receipt, planned_payload)
    _ = _complete_moves(selected, entries, restoring=False)
    receipt_value["state"] = "quarantined"
    _replace_bounded_file(selected, receipt, quarantined_payload)
    return receipt


def quarantine_expired(*, older_than_days: int, root: Path | None = None, now: datetime | None = None) -> Path | None:
    if type(older_than_days) is not int or older_than_days < 1:
        raise GuestImageError("retention age must be at least one day")
    selected = prepare_root(root)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _storage_lock(selected):
        _recover_transactions(selected)
        _validate_inventory(selected)
        return _quarantine_expired_locked(selected, older_than_days=older_than_days, current=current)


def _restore_quarantine_locked(selected: Path, receipt: Path, *, current: datetime) -> None:
    try:
        relative_receipt = receipt.relative_to(selected / "quarantine")
    except ValueError as exc:
        raise GuestImageError("quarantine receipt must be inside the guest image root") from exc
    if len(relative_receipt.parts) != 2 or relative_receipt.name != "receipt.json":
        raise GuestImageError("quarantine receipt path is invalid")
    value = _read_json(receipt)
    state, entries = _validated_quarantine_receipt(receipt.parent, value)
    if state not in {"quarantined", "restored"}:
        raise GuestImageError("quarantine receipt is not restorable")
    if state == "restored":
        return
    value["state"] = "restoring"
    value["restore_started_at"] = current.isoformat()
    restoring_payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    restored_value = dict(value)
    restored_value["state"] = "restored"
    restored_value["restored_at"] = current.isoformat()
    restored_value["retained_quarantined_duplicates"] = [str(entry["to"]) for entry in entries]
    maximum_restored_payload = (json.dumps(restored_value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _ensure_replacement_capacity(selected, receipt, (restoring_payload, maximum_restored_payload))
    _replace_bounded_file(selected, receipt, restoring_payload)
    retained_duplicates = _complete_moves(selected, entries, restoring=True)
    restored_value["retained_quarantined_duplicates"] = retained_duplicates
    restored_payload = (json.dumps(restored_value, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _replace_bounded_file(selected, receipt, restored_payload)


def restore_quarantine(receipt: Path, *, root: Path | None = None, now: datetime | None = None) -> None:
    selected = prepare_root(root)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _storage_lock(selected):
        _recover_transactions(selected)
        _validate_inventory(selected)
        _restore_quarantine_locked(selected, receipt, current=current)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve or explicitly quarantine validated guest images.")
    parser.add_argument("--root", type=Path, help="Override the owner-private runtime root.")
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve", help="Resolve one active guest image reference.")
    resolve.add_argument("--reference", required=True)
    resolve.add_argument("--service", choices=sorted(SUPPORTED_SERVICES), required=True)
    cleanup = commands.add_parser("cleanup", help="Move expired active batches into recoverable quarantine.")
    cleanup.add_argument("--older-than-days", type=int, required=True)
    restore = commands.add_parser("restore", help="Restore one exact quarantine receipt.")
    restore.add_argument("--receipt", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.command == "resolve":
            resolution = resolve_for_service(args.reference, service=args.service, root=args.root)
            print(
                json.dumps(
                    {
                        "schema": resolution.schema,
                        "service": resolution.service,
                        "reference": resolution.reference,
                        "path": str(resolution.path),
                        "sha256": resolution.sha256,
                        "mime_type": resolution.mime_type,
                        "size_bytes": resolution.size_bytes,
                        "batch_receipt": str(resolution.batch_receipt),
                        "batch_receipt_sha256": resolution.batch_receipt_sha256,
                        "sender": resolution.sender,
                        "route_target": resolution.route_target,
                        "authentication": resolution.authentication,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "cleanup":
            receipt = quarantine_expired(older_than_days=args.older_than_days, root=args.root)
            print(receipt or "no expired guest image batches")
        else:
            restore_quarantine(args.receipt, root=args.root)
            print("restored guest image quarantine")
    except GuestImageError as exc:
        print(f"omo_guest_images.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
