#!/usr/bin/env python3
"""Create a hash-bound tombstone for one historically cleared report transaction."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SCHEMA = "omo-report-historical-clear-tombstone/v1"
SENT_RE = re.compile(r"^\(sent from ([A-Za-z0-9_.-]+) via omo_report\.sh tmux=([A-Za-z][A-Za-z0-9_-]*:\d+(?:\.\d+)?) time=\S+ task-file=([A-Za-z0-9_.-]+)\)$")
MESSAGE_HASH_RE = re.compile(r"^\[message-sha256: ([0-9a-f]{64})\]$")
OWNER_RE = re.compile(r"^\[omo-report-owner-prefix: manager-path-sha256=([0-9a-f]{64}) sha256=([0-9a-f]{64}) size-bytes=(\d+) separator-bytes=([12])\]$")


class ReconcileError(RuntimeError):
    """The historical transaction cannot be safely reconciled."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


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


def reconcile(args: argparse.Namespace) -> dict[str, object]:
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
    if (args.receipt_directory / f"{args.replay_id}.json").exists() or (args.receipt_directory / f"{args.replay_id}.publication.json").exists():
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
    parser.add_argument("--report-key-sha256", required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manager-path", required=True)
    parser.add_argument("--before-revision", required=True)
    parser.add_argument("--after-revision", required=True)
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--producer-target", required=True)
    try:
        print(json.dumps(reconcile(parser.parse_args()), sort_keys=True))
    except (ReconcileError, OSError, ValueError) as exc:
        print(f"omo_report_reconcile.py: {exc}", file=os.sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
