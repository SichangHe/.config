#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pyyaml>=6.0.2"]
# ///
"""Durably acknowledge one fully processed PB wake on one pinned v1 task."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import sys
from urllib.parse import quote
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omo_manager.omo_agent_status import read_task_metadata
from omo_manager.omo_task_lock import task_file_lock, task_target_lock


NOTICE_ID = "7e6094f6391c199f"
TASK_REF = "202607/pbw_interpreter_live.md"
HANDOFF_KIND = "agent_handoff"
HANDOFF_EVENT = f"pb_handoff_2026-09-02T01:37:58+00:00_{NOTICE_ID}"
PROMPT_SHA256 = "50bb4e252715247b2192c86598197230a8c543fd969ecf8b3ff628ba7e0f0cba"
ITEM_DECISIONS = (
    ("pb_d66c039216bcbb8ac06b0454", "none"),
    ("pb_5386b6ca6552474966ca49ed", "urgent"),
    ("pb_c7ef054f54a31ecad589cc9f", "digest"),
    ("pb_0e89a7f0d21160196037b1a8", "none"),
    ("pb_fe67d95b9ee1a4a317707ea9", "none"),
    ("pb_521f9632a22d55331ed70ee8", "none"),
    ("pb_ebf3e85be35ba4763e183c1d", "urgent"),
    ("pb_b66da04ea53c030da926fb0d", "none"),
)
REPORT_IDS = (
    "pbmr_5bfbc48ac4d7bde6fd0d55df",
    "pbmr_8e6f6200550f039ec0ee1a05",
    "pbmr_4cb87eeaced0bf484d01774f",
    "pbmr_bc31163c0fa67fe42970ca59",
    "pbmr_9abcbc09d5707c0cc75eff7f",
)
SCHEMA = "omo-legacy-wake-ack/v1"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/omo-manager/legacy-wake-acks"


class AckError(RuntimeError):
    """The bounded acknowledgment failed closed."""


@dataclass(frozen=True)
class Args:
    root: Path
    task: str
    task_sha256: str
    expected_status: str
    expected_target: str
    expected_manager: str
    expected_queue_sha256: str
    todo_sha256: str
    notice_id: str
    database: Path
    database_dev: int
    database_ino: int
    state_root: Path = DEFAULT_STATE_ROOT


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def queue_sha256(items: tuple[str, ...]) -> str:
    return sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode())


def parse_args(argv: list[str]) -> Args:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--task-sha256", required=True)
    parser.add_argument("--expected-status", required=True)
    parser.add_argument("--expected-target", required=True)
    parser.add_argument("--expected-manager", required=True)
    parser.add_argument("--expected-queue-sha256", required=True)
    parser.add_argument("--todo-sha256", required=True)
    parser.add_argument("--notice-id", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--database-dev", type=int, required=True)
    parser.add_argument("--database-ino", type=int, required=True)
    parsed = parser.parse_args(argv)
    return Args(**vars(parsed))


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def load_bound_task(args: Args) -> tuple[bytes, tuple[str, ...]]:
    if args.task != TASK_REF or args.notice_id != NOTICE_ID:
        raise AckError("this helper accepts only the reviewed task and notice")
    path = args.root / args.task
    if path.resolve(strict=True) != args.root.resolve(strict=True) / TASK_REF:
        raise AckError("task path is not the reviewed in-root task")
    data = path.read_bytes()
    if sha256(data) != args.task_sha256:
        raise AckError("task bytes changed")
    metadata = read_task_metadata(path, args.root)
    if metadata is None or metadata.version != "v1.0.0" or metadata.is_manager:
        raise AckError("task is not the reviewed ordinary v1 task")
    if (metadata.status, metadata.runat, metadata.managerat) != (
        args.expected_status,
        args.expected_target,
        args.expected_manager,
    ):
        raise AckError("task status or ownership changed")
    queue = metadata.pending_task_items
    if queue_sha256(queue) != args.expected_queue_sha256:
        raise AckError("task queue changed")
    return data, queue


def verify_todo_and_ownership(args: Args) -> bytes:
    todo = args.root / "TODO.md"
    data = todo.read_bytes()
    if sha256(data) != args.todo_sha256:
        raise AckError("TODO bytes changed")
    expected = f"{TASK_REF} {args.expected_target}"
    current_rows: list[str] = []
    section = ""
    for raw_line in data.decode().splitlines():
        line = raw_line.strip()
        if line.endswith(":") and " " not in line[:-1]:
            section = line[:-1]
        elif section == "current" and line:
            current_rows.append(line)
    rows = [line for line in current_rows if line == expected]
    if len(rows) != 1:
        raise AckError("task does not have exactly one current TODO row")
    owners: list[str] = []
    for path in args.root.rglob("*.md"):
        if path.name == "TODO.md":
            continue
        metadata = read_task_metadata(path, args.root)
        if metadata is not None and metadata.status != "done" and metadata.runat == args.expected_target:
            owners.append(str(path.relative_to(args.root)))
    if owners != [TASK_REF]:
        raise AckError("target does not have exactly one active task owner")
    return data


def database_evidence(connection: sqlite3.Connection) -> dict[str, object]:
    connection.row_factory = sqlite3.Row
    event = connection.execute(
        "SELECT event_id, event_kind, created_at, detail_json FROM events WHERE event_id = ?",
        (HANDOFF_EVENT,),
    ).fetchone()
    if event is None or event["event_kind"] != HANDOFF_KIND:
        raise AckError("reviewed handoff event is absent")
    try:
        detail = json.loads(event["detail_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AckError("handoff event detail is invalid") from exc
    expected_ids = [item_id for item_id, _decision in ITEM_DECISIONS]
    if detail.get("item_ids") != expected_ids or detail.get("manager_report_ids") != list(REPORT_IDS) or detail.get("prompt_sha256") != PROMPT_SHA256 or not detail.get("submitted_at"):
        raise AckError("handoff event identity changed")

    items: list[dict[str, str]] = []
    for item_id, expected_decision in ITEM_DECISIONS:
        row = connection.execute(
            "SELECT item_id, agent_status, agent_decision, dispatch_status, agent_text FROM items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None or row["agent_status"] != "agent_handled":
            raise AckError(f"item is not durably handled: {item_id}")
        decision = row["agent_decision"] or "none"
        expected_dispatch = "none" if expected_decision == "none" else "dispatched"
        if decision != expected_decision or row["dispatch_status"] != expected_dispatch or not str(row["agent_text"] or "").strip():
            raise AckError(f"item decision evidence changed: {item_id}")
        items.append(
            {
                "item_id": item_id,
                "decision": decision,
                "dispatch_status": str(row["dispatch_status"]),
                "text_sha256": sha256(str(row["agent_text"]).encode()),
            }
        )
    placeholders = ",".join("?" for _ in expected_ids)
    candidate_count = connection.execute(
        f"SELECT count(*) FROM items WHERE item_id IN ({placeholders}) AND agent_status = 'collected'",
        expected_ids,
    ).fetchone()[0]
    if candidate_count:
        raise AckError("a reviewed item is still a current candidate")
    reports: list[dict[str, str]] = []
    for report_id in REPORT_IDS:
        rows = connection.execute(
            """SELECT report_id, report_status, delivery_status, delivered_at, report_text
               FROM manager_reports WHERE report_id = ?""",
            (report_id,),
        ).fetchall()
        if len(rows) != 1:
            raise AckError(f"manager report is not unique: {report_id}")
        row = rows[0]
        if row["delivery_status"] != "closed_consumed_unreceipted" or not row["delivered_at"]:
            raise AckError(f"manager report is not terminally closed: {report_id}")
        reports.append(
            {
                "report_id": report_id,
                "report_status": str(row["report_status"]),
                "delivery_status": str(row["delivery_status"]),
                "delivered_at": str(row["delivered_at"]),
                "text_sha256": sha256(str(row["report_text"]).encode()),
            }
        )
    return {
        "event_id": event["event_id"],
        "event_created_at": event["created_at"],
        "prompt_sha256": PROMPT_SHA256,
        "items": items,
        "manager_reports": reports,
    }


def publish_receipt(path: Path, payload: bytes) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if not path.parent.is_dir() or parent_info.st_uid != os.getuid() or parent_info.st_mode & 0o077:
        raise AckError("acknowledgment state directory is unsafe")
    lock_path = path.with_name(f".{path.name}.lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            existing_info = path.lstat()
        except FileNotFoundError:
            existing_info = None
        if existing_info is not None:
            if not stat.S_ISREG(existing_info.st_mode) or existing_info.st_uid != os.getuid() or existing_info.st_mode & 0o777 != 0o600:
                raise AckError("existing acknowledgment receipt is unsafe")
            existing_fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                opened_info = os.fstat(existing_fd)
                if (opened_info.st_dev, opened_info.st_ino) != (existing_info.st_dev, existing_info.st_ino):
                    raise AckError("existing acknowledgment receipt changed during open")
                with os.fdopen(existing_fd, "rb", closefd=True) as stream:
                    existing = stream.read()
                existing_fd = -1
            finally:
                if existing_fd >= 0:
                    os.close(existing_fd)
            if existing != payload:
                raise AckError("an incompatible acknowledgment receipt already exists")
            return "already-acknowledged"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return "acknowledged"
    finally:
        os.close(lock_fd)


def run(args: Args) -> str:
    task_path = args.root / args.task
    receipt = args.state_root / f"{NOTICE_ID}.json"
    if receipt.parent != args.state_root or args.state_root.is_symlink():
        raise AckError("acknowledgment receipt path is unsafe")
    if args.state_root.exists():
        state_info = args.state_root.lstat()
        if not args.state_root.is_dir() or state_info.st_uid != os.getuid() or state_info.st_mode & 0o077:
            raise AckError("acknowledgment state directory is unsafe")
    with ExitStack() as stack:
        stack.enter_context(task_file_lock(task_path))
        stack.enter_context(task_target_lock(args.root, args.expected_target))
        task_data, queue = load_bound_task(args)
        todo_data = verify_todo_and_ownership(args)
        database_info = args.database.stat()
        if (database_info.st_dev, database_info.st_ino) != (args.database_dev, args.database_ino):
            raise AckError("PB database identity changed")
        database_fd = os.open(args.database, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        stack.callback(os.close, database_fd)
        opened_info = os.fstat(database_fd)
        if (opened_info.st_dev, opened_info.st_ino) != (args.database_dev, args.database_ino):
            raise AckError("PB database descriptor identity changed")
        database_uri = f"file:{quote(f'/proc/self/fd/{database_fd}')}?mode=rw"
        connection = sqlite3.connect(database_uri, uri=True, isolation_level=None)
        stack.callback(connection.close)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA query_only = ON")
        evidence = database_evidence(connection)
        current_task, current_queue = load_bound_task(args)
        if current_task != task_data or current_queue != queue:
            raise AckError("task changed during acknowledgment")
        if verify_todo_and_ownership(args) != todo_data:
            raise AckError("TODO or task ownership changed during acknowledgment")
        current_info = args.database.stat()
        if (current_info.st_dev, current_info.st_ino) != (args.database_dev, args.database_ino):
            raise AckError("PB database changed during acknowledgment")
        payload = canonical_json(
            {
                "schema": SCHEMA,
                "notice_id": args.notice_id,
                "task": args.task,
                "task_sha256": args.task_sha256,
                "status": args.expected_status,
                "target": args.expected_target,
                "manager": args.expected_manager,
                "queue_sha256": args.expected_queue_sha256,
                "todo_sha256": args.todo_sha256,
                "database_dev": args.database_dev,
                "database_ino": args.database_ino,
                "evidence": evidence,
            }
        )
        result = publish_receipt(receipt, payload)
        connection.execute("COMMIT")
        return result


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(sys.argv[1:] if argv is None else argv))
    except (AckError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
