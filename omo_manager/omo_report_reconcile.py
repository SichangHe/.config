#!/usr/bin/env python3
"""Create a hash-bound tombstone for one historically cleared report transaction."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "omo-report-historical-clear-tombstone/v1"
DISCARD_SCHEMA = "omo-report-authorized-discard/v1"
LOCAL_ENV_PATH = Path(__file__).with_name("local.env")
SENT_RE = re.compile(r"^\(sent from ([A-Za-z0-9_.-]+) via omo_report\.sh tmux=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) time=\S+ task-file=([A-Za-z0-9_.-]+)\)$")
MESSAGE_HASH_RE = re.compile(r"^\[message-sha256: ([0-9a-f]{64})\]$")
OWNER_RE = re.compile(r"^\[omo-report-owner-prefix: manager-path-sha256=([0-9a-f]{64}) sha256=([0-9a-f]{64}) size-bytes=(\d+) separator-bytes=([12])\]$")


class ReconcileError(RuntimeError):
    """The historical transaction cannot be safely reconciled."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def configured_mail_root() -> Path:
    """Resolve the mail root from the fixed, owner-controlled manager configuration."""

    try:
        info = LOCAL_ENV_PATH.lstat()
        payload = LOCAL_ENV_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReconcileError("trusted manager configuration is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ReconcileError("trusted manager configuration is unsafe")
    matches = re.findall(r'^export OMO_WORK_LOGS_ROOT="([^"\n]+)"$', payload, flags=re.MULTILINE)
    if len(matches) != 1 or not Path(matches[0]).is_absolute():
        raise ReconcileError("trusted manager mail root is not configured exactly once")
    return Path(matches[0]) / "manager_mail"


def name_exists(path: Path) -> bool:
    """Return whether a directory entry exists, including dangling symlinks."""

    return os.path.lexists(path)


def read_exact_private_file(path: Path, *, mode: int | None = None) -> tuple[bytes, os.stat_result]:
    """Read one owned regular file through a no-follow descriptor."""

    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
        ):
            raise ReconcileError("private file is unsafe")
        with os.fdopen(os.dup(fd), "rb") as stream:
            return stream.read(), info
    finally:
        os.close(fd)


def require_nonsymlink_directory(path: Path) -> Path:
    """Resolve an absolute directory only when every path component is a directory."""

    if not path.is_absolute():
        raise ReconcileError("trusted directory is not absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ReconcileError("trusted directory is unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ReconcileError("trusted directory contains a non-directory component")
    return current


def git_blob(repo: Path, revision: str, relative_path: str) -> bytes:
    if not revision or revision.startswith("-") or "\0" in revision or Path(relative_path).is_absolute():
        raise ReconcileError("historical Git binding is invalid")
    result = subprocess.run(
        ["git", "show", f"{revision}:{relative_path}"],
        cwd=repo,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ReconcileError("historical Git evidence is unavailable")
    return result.stdout


def load_commitment(path: Path, replay_id: str) -> dict[str, object]:
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError("transaction commitment is unreadable") from exc
    if not isinstance(value, dict) or canonical_json(value) != payload or value.get("replay_id") != replay_id:
        raise ReconcileError("transaction commitment identity is invalid")
    unsigned = dict(value)
    commitment_id = unsigned.pop("commitment_id", None)
    if commitment_id != hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest():
        raise ReconcileError("transaction commitment signature is invalid")
    return value


def load_private_commitment(path: Path, replay_id: str) -> tuple[dict[str, object], bytes, os.stat_result]:
    """Load one exact owner-private commitment without following links."""

    try:
        payload, info = read_exact_private_file(path, mode=0o600)
        value = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReconcileError) as exc:
        raise ReconcileError("transaction commitment is unreadable or unsafe") from exc
    if not isinstance(value, dict) or canonical_json(value) != payload or value.get("replay_id") != replay_id:
        raise ReconcileError("transaction commitment identity is invalid")
    unsigned = dict(value)
    commitment_id = unsigned.pop("commitment_id", None)
    if commitment_id != hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest():
        raise ReconcileError("transaction commitment signature is invalid")
    return value, payload, info


@contextmanager
def report_transaction_lock(manager: Path) -> Iterator[None]:
    """Share the receipt writer's adjacent per-manager transaction lock."""

    lock_path = Path(f"{manager}.omo_report.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ReconcileError("report transaction lock is unsafe")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def discard_commitment(args: argparse.Namespace, *, locked: bool = False) -> dict[str, object]:
    """Retire one exact commitment after an explicit human disposition."""

    if HASH_RE.fullmatch(args.replay_id) is None or HASH_RE.fullmatch(args.commitment_id or "") is None:
        raise ReconcileError("discard identity is invalid")
    if re.fullmatch(r"manager_mail/[A-Za-z0-9_.-]+\.txt", args.authorization_source or "") is None:
        raise ReconcileError("discard authorization source is invalid")
    authorization_file = args.authorization_file
    try:
        mail_root = require_nonsymlink_directory(configured_mail_root())
        expected_authorization = mail_root / Path(args.authorization_source).name
        authorization_payload, _authorization_info = read_exact_private_file(authorization_file)
    except (OSError, ReconcileError) as exc:
        raise ReconcileError("discard authorization source is unavailable") from exc
    if (
        not authorization_file.is_absolute()
        or authorization_file != expected_authorization
        or authorization_file.name != Path(args.authorization_source).name
        or HASH_RE.fullmatch(args.authorization_sha256 or "") is None
        or hashlib.sha256(authorization_payload).hexdigest() != args.authorization_sha256
        or args.replay_id.encode() not in authorization_payload
        or b"Consider it done, and make it go away." not in authorization_payload
    ):
        raise ReconcileError("discard authorization source is not the exact human disposition")
    directory_info = args.receipt_directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise ReconcileError("receipt directory is not owner-private")
    commitment_path = args.receipt_directory / f"{args.replay_id}.commitment"
    disposition_path = args.receipt_directory / f"{args.replay_id}.discarded.json"
    lock_path = args.receipt_directory / ".reconcile.lock"
    lock_fd = None if locked else os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if lock_fd is not None:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if name_exists(disposition_path) and not name_exists(commitment_path):
            info = disposition_path.lstat()
            try:
                record = json.loads(disposition_path.read_bytes())
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReconcileError("existing discard disposition is unreadable") from exc
            unsigned = dict(record) if isinstance(record, dict) else {}
            disposition_id = unsigned.pop("disposition_id", None)
            expected_keys = {
                "authorization_source", "authorization_sha256", "commitment_id", "commitment_sha256",
                "disposition_id", "replay_id", "schema", "terminal",
            }
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not isinstance(record, dict)
                or set(record) != expected_keys
                or canonical_json(record) != disposition_path.read_bytes()
                or record.get("schema") != DISCARD_SCHEMA
                or record.get("terminal") is not True
                or record.get("replay_id") != args.replay_id
                or record.get("commitment_id") != args.commitment_id
                or record.get("authorization_source") != args.authorization_source
                or record.get("authorization_sha256") != args.authorization_sha256
                or HASH_RE.fullmatch(str(record.get("commitment_sha256", ""))) is None
                or disposition_id != hashlib.sha256(canonical_json(unsigned).rstrip(b"\n")).hexdigest()
            ):
                raise ReconcileError("existing discard disposition differs from the exact authorization")
            return record
        commitment, commitment_payload, commitment_info = load_private_commitment(commitment_path, args.replay_id)
        preflight = commitment.get("preflight")
        records = preflight.get("records") if isinstance(preflight, dict) else None
        manager_value = records.get("manager") if isinstance(records, dict) else None
        if not isinstance(manager_value, str) or not manager_value or not Path(manager_value).is_absolute():
            raise ReconcileError("transaction commitment has no manager lock binding")
        manager = Path(manager_value).resolve(strict=False)
        if not locked:
            with report_transaction_lock(manager):
                return discard_commitment(args, locked=True)
        if commitment.get("commitment_id") != args.commitment_id:
            raise ReconcileError("discard commitment ID differs from the exact authorization")
        for suffix in (".json", ".publication.json", ".tombstone.json"):
            if name_exists(args.receipt_directory / f"{args.replay_id}{suffix}"):
                raise ReconcileError("transaction already has a terminal receipt or reconciliation")
        record: dict[str, object] = {
            "authorization_source": args.authorization_source,
            "authorization_sha256": args.authorization_sha256,
            "commitment_id": args.commitment_id,
            "commitment_sha256": hashlib.sha256(commitment_payload).hexdigest(),
            "replay_id": args.replay_id,
            "schema": DISCARD_SCHEMA,
            "terminal": True,
        }
        record["disposition_id"] = hashlib.sha256(canonical_json(record).rstrip(b"\n")).hexdigest()
        payload = canonical_json(record)
        if name_exists(disposition_path):
            info = disposition_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ReconcileError("existing discard disposition is not owner-private")
            if disposition_path.read_bytes() != payload:
                raise ReconcileError("existing discard disposition differs from the exact authorization")
        else:
            fd, temporary = tempfile.mkstemp(prefix=f".{args.replay_id}.", suffix=".tmp", dir=args.receipt_directory)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    fd = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, disposition_path, follow_symlinks=False)
            finally:
                if fd >= 0:
                    os.close(fd)
                Path(temporary).unlink(missing_ok=True)
            directory_fd = os.open(args.receipt_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        try:
            current_payload, current_info = read_exact_private_file(commitment_path, mode=0o600)
            directory_entry = commitment_path.lstat()
        except (OSError, ReconcileError) as exc:
            raise ReconcileError("transaction commitment changed before retirement") from exc
        if (
            current_payload != commitment_payload
            or (current_info.st_dev, current_info.st_ino) != (commitment_info.st_dev, commitment_info.st_ino)
            or (directory_entry.st_dev, directory_entry.st_ino) != (commitment_info.st_dev, commitment_info.st_ino)
            or not stat.S_ISREG(directory_entry.st_mode)
        ):
            raise ReconcileError("transaction commitment changed before retirement")
        commitment_path.unlink()
        directory_fd = os.open(args.receipt_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return record
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def reconcile(args: argparse.Namespace, *, locked: bool = False) -> dict[str, object]:
    if HASH_RE.fullmatch(args.replay_id) is None or HASH_RE.fullmatch(args.report_key_sha256) is None:
        raise ReconcileError("reconciliation hashes are invalid")
    commitment_path = args.receipt_directory / f"{args.replay_id}.commitment"
    directory_info = args.receipt_directory.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.getuid() or stat.S_IMODE(directory_info.st_mode) != 0o700:
        raise ReconcileError("receipt directory is not owner-private")
    commitment = load_commitment(commitment_path, args.replay_id)
    preflight = commitment.get("preflight")
    records = preflight.get("records") if isinstance(preflight, dict) else None
    owner = preflight.get("owner_prefix") if isinstance(preflight, dict) else None
    if not isinstance(records, dict) or not isinstance(owner, dict):
        raise ReconcileError("transaction commitment binding is incomplete")
    expected_records = {
        "acknowledgment_ledger",
        "authority_completion",
        "manager",
        "private_envelope",
        "private_receipt",
        "producer",
        "receipt_publication",
        "transaction_commitment",
    }
    if set(records) != expected_records:
        raise ReconcileError("transaction commitment records are malformed")
    manager = Path(str(records["manager"])).resolve(strict=False)
    envelope = Path(str(records["private_envelope"])).resolve(strict=False)
    producer = Path(str(records["producer"])).resolve(strict=False)
    if not locked:
        with report_transaction_lock(manager):
            return reconcile(args, locked=True)
    if name_exists(args.receipt_directory / f"{args.replay_id}.discarded.json"):
        raise ReconcileError("transaction was explicitly discarded")
    if manager != (args.repo / args.manager_path).resolve(strict=False) or envelope != args.envelope.resolve(strict=False):
        raise ReconcileError("explicit historical paths do not match the commitment")
    if not producer.is_relative_to(args.repo.resolve(strict=False)):
        raise ReconcileError("committed producer escapes the repository")
    if records["transaction_commitment"] != str(commitment_path):
        raise ReconcileError("transaction commitment path is inconsistent")
    if (
        set(owner) != {"manager_path_sha256", "separator_bytes", "sha256", "size_bytes"}
        or owner.get("manager_path_sha256") != hashlib.sha256(str(manager).encode()).hexdigest()
        or owner.get("separator_bytes") not in {1, 2}
        or not isinstance(owner.get("size_bytes"), int)
        or isinstance(owner.get("size_bytes"), bool)
        or int(owner["size_bytes"]) < 0
        or not isinstance(owner.get("sha256"), str)
        or HASH_RE.fullmatch(str(owner["sha256"])) is None
    ):
        raise ReconcileError("committed owner-prefix binding is invalid")
    if name_exists(args.receipt_directory / f"{args.replay_id}.json") or name_exists(args.receipt_directory / f"{args.replay_id}.publication.json"):
        raise ReconcileError("transaction already has a durable receipt")
    try:
        envelope_payload = envelope.read_bytes()
    except OSError as exc:
        raise ReconcileError("bound private envelope is unavailable") from exc
    header, separator, message = envelope_payload.partition(b"message:\n")
    try:
        header_lines = header.decode().splitlines()
        _ = message.decode()
    except UnicodeDecodeError as exc:
        raise ReconcileError("private envelope is not UTF-8") from exc
    if not separator or len(header_lines) not in {3, 5}:
        raise ReconcileError("private envelope header is malformed")
    sent = SENT_RE.fullmatch(header_lines[0])
    message_hash = MESSAGE_HASH_RE.fullmatch(header_lines[1])
    owner_header = OWNER_RE.fullmatch(header_lines[2])
    if sent is None or message_hash is None or owner_header is None:
        raise ReconcileError("private envelope binding is malformed")
    allocation = commitment.get("allocation")
    file_state = allocation.get("file_at_submission") if isinstance(allocation, dict) else None
    if (
        sent.group(2) != args.producer_target
        or sent.group(3) != producer.name
        or message_hash.group(1) != hashlib.sha256(message).hexdigest()
        or not isinstance(file_state, dict)
        or file_state.get("sha256") != message_hash.group(1)
        or file_state.get("size") != len(message)
        or owner_header.groups()
        != (owner.get("manager_path_sha256"), owner.get("sha256"), str(owner.get("size_bytes")), str(owner.get("separator_bytes")))
    ):
        raise ReconcileError("private envelope differs from the committed transaction")
    before = git_blob(args.repo, args.before_revision, args.manager_path)
    after = git_blob(args.repo, args.after_revision, args.manager_path)
    pointer = f"(from agent {args.producer_target} {envelope})".encode()
    suffix = b"\n" * int(owner.get("separator_bytes", -1)) + b"(pending)\n" + pointer + b"\n"
    owner_size = owner.get("size_bytes")
    owner_sha = owner.get("sha256")
    if not isinstance(owner_size, int) or hashlib.sha256(after).hexdigest() != owner_sha or len(after) != owner_size:
        raise ReconcileError("historical after-state does not restore the committed owner bytes")
    if before != after + suffix:
        raise ReconcileError("historical before-state is not the exact committed pointer append")
    if manager.exists() and pointer in manager.read_bytes():
        raise ReconcileError("report pointer is currently active")
    record: dict[str, object] = {
        "after": {"git_revision": args.after_revision, "sha256": hashlib.sha256(after).hexdigest(), "size_bytes": len(after)},
        "before": {"git_revision": args.before_revision, "sha256": hashlib.sha256(before).hexdigest(), "size_bytes": len(before)},
        "commitment_id": commitment["commitment_id"],
        "envelope_path": str(envelope),
        "envelope_sha256": hashlib.sha256(envelope_payload).hexdigest(),
        "manager_path": str(manager),
        "manager_path_sha256": hashlib.sha256(str(manager).encode()).hexdigest(),
        "owner_prefix_sha256": owner_sha,
        "pointer_sha256": hashlib.sha256(pointer).hexdigest(),
        "replay_id": args.replay_id,
        "report_key_sha256": args.report_key_sha256,
        "schema": SCHEMA,
        "terminal": True,
    }
    record["tombstone_id"] = hashlib.sha256(canonical_json(record).rstrip(b"\n")).hexdigest()
    output = args.receipt_directory / f"{args.replay_id}.tombstone.json"
    payload = canonical_json(record)
    if output.exists():
        output_info = output.lstat()
        if not stat.S_ISREG(output_info.st_mode) or output_info.st_uid != os.getuid() or stat.S_IMODE(output_info.st_mode) != 0o600:
            raise ReconcileError("existing tombstone is not owner-private")
        if output.read_bytes() != payload:
            raise ReconcileError("existing tombstone differs from the exact reconciliation")
        return record
    fd, temporary = tempfile.mkstemp(prefix=f".{args.replay_id}.", suffix=".tmp", dir=args.receipt_directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError:
            output_info = output.lstat()
            if not stat.S_ISREG(output_info.st_mode) or output_info.st_uid != os.getuid() or stat.S_IMODE(output_info.st_mode) != 0o600:
                raise ReconcileError("competing tombstone is not owner-private")
            if output.read_bytes() != payload:
                raise ReconcileError("competing tombstone differs from the exact reconciliation")
        directory_fd = os.open(args.receipt_directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-directory", type=Path, required=True)
    parser.add_argument("--replay-id", required=True)
    parser.add_argument("--report-key-sha256")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--manager-path")
    parser.add_argument("--before-revision")
    parser.add_argument("--after-revision")
    parser.add_argument("--envelope", type=Path)
    parser.add_argument("--producer-target")
    parser.add_argument("--discard-authorized", action="store_true")
    parser.add_argument("--commitment-id")
    parser.add_argument("--authorization-source")
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--authorization-sha256")
    try:
        args = parser.parse_args()
        required = (args.report_key_sha256, args.repo, args.manager_path, args.before_revision, args.after_revision, args.envelope, args.producer_target)
        if args.discard_authorized:
            if any(required) or args.authorization_file is None:
                raise ReconcileError("discard mode cannot be combined with historical reconciliation")
            result = discard_commitment(args)
        else:
            if not all(required) or args.commitment_id or args.authorization_source or args.authorization_file or args.authorization_sha256:
                raise ReconcileError("historical reconciliation arguments are incomplete")
            result = reconcile(args)
        print(json.dumps(result, sort_keys=True))
    except (ReconcileError, OSError, ValueError) as exc:
        print(f"omo_report_reconcile.py: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
