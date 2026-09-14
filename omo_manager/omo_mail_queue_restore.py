#!/usr/bin/env python3
"""Restore one authenticated historical `mail_cleanup_w.md` queue subset."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_task_lock import task_target_lock
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_status import same_file_state

# 🧑 "Bind the authenticated prior commit `61d4e775a19d105085ba62fd97f1724a1ce3e217` and prior task SHA-256 `1aaf3b70b18c7668fb4b2e132a32045cd13c6c486e2719d155306600d7a5248c`"
PRIOR_COMMIT = "61d4e775a19d105085ba62fd97f1724a1ce3e217"
PRIOR_TASK_SHA256 = "1aaf3b70b18c7668fb4b2e132a32045cd13c6c486e2719d155306600d7a5248c"
TASK_NAME = "mail_cleanup_w.md"
TASK_TARGET = "wl:123"
TASK_MANAGER = "pb:1"
N_HISTORICAL_ITEMS = 47
N_RECONCILED_NEWEST = 2
RECONCILED_NEWEST_IDS = ("ccc3a4eb1d707f4f591a1527cb16c0fa", "07a057b0882d1e9a23a26c62cd627838")
CURRENT_ITEMS = (
    "email_idle_watcher total-cleanup threshold 1353cb6e5c0d495dea96da9300ab5d13: retained manager mail 64 exceeds 29; compress task by task regardless of read state, exclude PB digest streams from removal, and retain current requests, decisions, results, and status",
    "email_idle_watcher total-cleanup threshold ab8b4cc18be4e85e25c508c41d90e769: retained manager mail 65 exceeds 29; compress task by task regardless of read state, exclude PB digest streams from removal, and retain current requests, decisions, results, and status",
)
ITEM_ID_RE = re.compile(r"\bthreshold ([0-9a-f]{32}):")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RestoreError(Exception):
    """The authenticated restoration preconditions did not all hold."""


@dataclass(frozen=True)
class QueueBlock:
    items_start: int
    items_end: int
    item_lines: tuple[bytes, ...]
    items: tuple[str, ...]


@dataclass(frozen=True)
class RestoreResult:
    prior_sha256: str
    current_sha256: str
    restored_sha256: str
    restored_item_ids: tuple[str, ...]
    reconciled_item_ids: tuple[str, ...]
    current_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class Args:
    root: Path
    expected_current_sha256: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def canonical_git_root(root: Path) -> Path:
    if not root.is_absolute():
        raise RestoreError("--root must be absolute")
    canonical = root.resolve(strict=True)
    top = git(canonical, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if Path(top).resolve(strict=True) != canonical:
        raise RestoreError("--root does not identify the complete work-log Git root")
    return canonical


def authenticated_prior_bytes(root: Path) -> bytes:
    resolved = git(root, "rev-parse", "--verify", f"{PRIOR_COMMIT}^{{commit}}").stdout.decode().strip()
    if resolved != PRIOR_COMMIT:
        raise RestoreError("authenticated prior commit does not resolve exactly")
    ancestry = git(root, "merge-base", "--is-ancestor", PRIOR_COMMIT, "HEAD", check=False)
    if ancestry.returncode != 0:
        raise RestoreError("authenticated prior commit is not in current HEAD history")
    entry = git(root, "ls-tree", "-z", PRIOR_COMMIT, "--", TASK_NAME).stdout
    fields = entry.rstrip(b"\0").split(b"\t")
    if len(fields) != 2 or fields[1] != TASK_NAME.encode() or not fields[0].startswith((b"100644 blob ", b"100755 blob ")):
        raise RestoreError("authenticated prior task path is missing or has the wrong Git object type")
    data = git(root, "cat-file", "blob", f"{PRIOR_COMMIT}:{TASK_NAME}").stdout
    if sha256(data) != PRIOR_TASK_SHA256:
        raise RestoreError("authenticated prior task digest does not match")
    return data


def queue_block(data: bytes, root: Path) -> QueueBlock:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RestoreError("task bytes are not UTF-8") from error
    lines = data.splitlines(keepends=True)
    if not lines or lines[0] != b"---\n":
        raise RestoreError("task does not have canonical LF frontmatter")
    try:
        closing_idx = lines.index(b"---\n", 1)
    except ValueError as error:
        raise RestoreError("task frontmatter is not closed canonically") from error
    field_indexes = [idx for idx in range(1, closing_idx) if lines[idx].startswith(b"pending_task_items:")]
    if len(field_indexes) != 1 or lines[field_indexes[0]] != b"pending_task_items:\n":
        raise RestoreError("task must have one canonical nonempty pending queue")
    field_idx = field_indexes[0]
    end_idx = field_idx + 1
    while end_idx < closing_idx and lines[end_idx].startswith(b"  - "):
        end_idx += 1
    item_lines = tuple(lines[field_idx + 1 : end_idx])
    if not item_lines:
        raise RestoreError("task pending queue is empty")
    try:
        metadata = parse_task_metadata(text, root)
    except TaskFrontmatterError as error:
        raise RestoreError(f"task frontmatter is invalid: {error}") from error
    if metadata is None or len(metadata.pending_task_items) != len(item_lines):
        raise RestoreError("raw pending queue does not match parsed item set")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return QueueBlock(offsets[field_idx + 1], offsets[end_idx], item_lines, metadata.pending_task_items)


def item_ids(items: tuple[str, ...]) -> tuple[str, ...]:
    ids: list[str] = []
    for item in items:
        match = ITEM_ID_RE.search(item)
        if match is None or ITEM_ID_RE.search(item, match.end()) is not None:
            raise RestoreError("every pending item must contain exactly one threshold ID")
        ids.append(match.group(1))
    if len(ids) != len(set(ids)):
        raise RestoreError("pending threshold IDs are not unique")
    return tuple(ids)


def task_identity(data: bytes, root: Path) -> tuple[str, str, str, str, bool, str]:
    metadata = parse_task_metadata(data.decode("utf-8"), root)
    if metadata is None:
        raise RestoreError("task metadata is missing")
    return metadata.version, metadata.runat, metadata.tool, metadata.managerat, metadata.is_manager, metadata.session_id


def restored_bytes(prior: bytes, current: bytes, root: Path) -> tuple[bytes, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    prior_block = queue_block(prior, root)
    current_block = queue_block(current, root)
    if task_identity(prior, root) != task_identity(current, root):
        raise RestoreError("historical and current task identities do not match")
    version, target, tool, manager, is_manager, session_id = task_identity(current, root)
    if (version, target, tool, manager, is_manager) != ("v1.0.0", TASK_TARGET, "codex", TASK_MANAGER, False) or not session_id:
        raise RestoreError("current task identity is outside the supported restoration")
    if len(prior_block.items) != N_HISTORICAL_ITEMS:
        raise RestoreError("authenticated prior queue does not contain exactly 47 items")
    prior_ids = item_ids(prior_block.items)
    current_ids = item_ids(current_block.items)
    selected_items = prior_block.items[:-N_RECONCILED_NEWEST]
    selected_ids = prior_ids[:-N_RECONCILED_NEWEST]
    reconciled_ids = prior_ids[-N_RECONCILED_NEWEST:]
    if len(selected_items) != 45 or reconciled_ids != RECONCILED_NEWEST_IDS or current_block.items != CURRENT_ITEMS or set(current_ids) & set(prior_ids):
        raise RestoreError("derived historical subset or current item set is invalid")
    updated = current[: current_block.items_start] + b"".join(prior_block.item_lines[:45]) + current[current_block.items_start :]
    updated_block = queue_block(updated, root)
    if updated_block.items != (*selected_items, *current_block.items):
        raise RestoreError("restored pending queue order or item set is invalid")
    if updated[updated_block.items_end :] != current[current_block.items_end :]:
        raise RestoreError("restoration changed bytes after the current pending queue")
    return updated, selected_ids, reconciled_ids, current_ids


def atomic_replace_locked(path: Path, payload: bytes, before: os.stat_result, expected: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            _ = handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), before.st_mode & 0o7777)
        after = path.lstat()
        if not same_file_state(before, after) or not stat.S_ISREG(after.st_mode) or path.read_bytes() != expected:
            raise RestoreError("current task bytes changed before exact-CAS replacement")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(root: Path, expected_current_sha256: str) -> RestoreResult:
    if SHA256_RE.fullmatch(expected_current_sha256) is None:
        raise RestoreError("--expected-current-sha256 must be a lowercase SHA-256 digest")
    canonical = canonical_git_root(root)
    path = canonical / TASK_NAME
    if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
        raise RestoreError("current task path is not the exact regular file")
    with task_target_lock(canonical, TASK_TARGET), task_file_lock(path):
        prior = authenticated_prior_bytes(canonical)
        before = path.lstat()
        current = path.read_bytes()
        if not same_file_state(before, path.lstat()):
            raise RestoreError("current task changed while its execution bytes were read")
        current_sha256 = sha256(current)
        if current_sha256 != expected_current_sha256:
            raise RestoreError("current task digest does not match the fresh execution binding")
        updated, selected_ids, reconciled_ids, current_ids = restored_bytes(prior, current, canonical)
        atomic_replace_locked(path, updated, before, current)
    return RestoreResult(
        PRIOR_TASK_SHA256,
        current_sha256,
        sha256(updated),
        selected_ids,
        reconciled_ids,
        current_ids,
    )


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--expected-current-sha256", required=True)
    parsed: dict[str, object] = vars(parser.parse_args(argv))
    root = parsed.get("root")
    expected = parsed.get("expected_current_sha256")
    if not isinstance(root, Path) or not isinstance(expected, str):
        parser.error("invalid restoration arguments")
    return Args(root, expected)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        result = restore(args.root, args.expected_current_sha256)
    except (OSError, RestoreError, subprocess.SubprocessError, UnicodeError, TaskFrontmatterError) as error:
        print(f"omo_mail_queue_restore: {error}", file=sys.stderr)
        return 1
    restored_count = len(result.restored_item_ids)
    current_count = len(result.current_item_ids)
    reconciled = ",".join(result.reconciled_item_ids)
    current = ",".join(result.current_item_ids)
    print(
        f"restored_items={restored_count} current_items={current_count} total_items={restored_count + current_count} sha256={result.restored_sha256} reconciled_newest={reconciled} current_item_ids={current}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
