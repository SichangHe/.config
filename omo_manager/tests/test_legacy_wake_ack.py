from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from omo_manager import omo_legacy_wake_ack as subject


def task_text(queue: tuple[str, ...] = ("watch", "policy")) -> str:
    rows = "\n".join(f"  - {item}" for item in queue)
    return f"""---
version: v1.0.0
status: running
runat: pb:9
tool: codex
managerat: wl:2
is_manager: false
pending_task_items:
{rows}
---
body
"""


def create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE events(event_id TEXT PRIMARY KEY, item_id TEXT, event_kind TEXT, created_at TEXT, detail_json TEXT);
        CREATE TABLE items(
          item_id TEXT PRIMARY KEY, agent_status TEXT, agent_decision TEXT,
          dispatch_status TEXT, agent_text TEXT
        );
        CREATE TABLE manager_reports(
          report_id TEXT PRIMARY KEY, report_status TEXT, delivery_status TEXT,
          delivered_at TEXT, report_text TEXT
        );
        """
    )
    detail = {
        "item_ids": [item_id for item_id, _decision in subject.ITEM_DECISIONS],
        "manager_report_ids": list(subject.REPORT_IDS),
        "prompt_sha256": subject.PROMPT_SHA256,
        "submitted_at": "2026-09-02T01:38:20+00:00",
    }
    connection.execute(
        "INSERT INTO events VALUES(?, NULL, ?, ?, ?)",
        (subject.HANDOFF_EVENT, subject.HANDOFF_KIND, "2026-09-02T01:37:58+00:00", json.dumps(detail)),
    )
    for item_id, decision in subject.ITEM_DECISIONS:
        stored = None if decision == "none" else decision
        dispatch = "none" if decision == "none" else "dispatched"
        connection.execute("INSERT INTO items VALUES(?, 'agent_handled', ?, ?, 'evidence')", (item_id, stored, dispatch))
    for report_id in subject.REPORT_IDS:
        connection.execute(
            "INSERT INTO manager_reports VALUES(?, 'blocked', 'closed_consumed_unreceipted', 'now', 'evidence')",
            (report_id,),
        )
    connection.commit()
    connection.close()


def bound_args(tmp_path: Path) -> subject.Args:
    root = tmp_path / "work_logs"
    task = root / subject.TASK_REF
    task.parent.mkdir(parents=True)
    data = task_text().encode()
    task.write_bytes(data)
    todo = root / "TODO.md"
    todo.write_text(f"current:\n{subject.TASK_REF} pb:9\n\nprevious:\n", encoding="utf-8")
    database = tmp_path / "pb.sqlite"
    create_database(database)
    info = database.stat()
    return subject.Args(
        root=root,
        task=subject.TASK_REF,
        task_sha256=hashlib.sha256(data).hexdigest(),
        expected_status="running",
        expected_target="pb:9",
        expected_manager="wl:2",
        expected_queue_sha256=subject.queue_sha256(("watch", "policy")),
        todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest(),
        notice_id=subject.NOTICE_ID,
        database=database,
        database_dev=info.st_dev,
        database_ino=info.st_ino,
        state_root=tmp_path / "state",
    )


def test_exact_success_is_durable_idempotent_and_non_mutating(tmp_path: Path) -> None:
    args = bound_args(tmp_path)
    task_before = (args.root / args.task).read_bytes()
    db_before = args.database.read_bytes()
    assert subject.run(args) == "acknowledged"
    assert subject.run(args) == "already-acknowledged"
    assert (args.root / args.task).read_bytes() == task_before
    assert args.database.read_bytes() == db_before
    receipt = args.state_root / f"{subject.NOTICE_ID}.json"
    payload = json.loads(receipt.read_text())
    assert payload["notice_id"] == subject.NOTICE_ID
    assert len(payload["evidence"]["items"]) == 8
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("field", "value"),
    (("notice_id", "wrong"), ("task", "other.md"), ("expected_manager", "wl:6"), ("expected_queue_sha256", "0" * 64)),
)
def test_rejects_identity_ownership_and_queue_drift(tmp_path: Path, field: str, value: str) -> None:
    args = replace(bound_args(tmp_path), **{field: value})
    with pytest.raises(subject.AckError):
        subject.run(args)
    assert not (args.state_root / f"{subject.NOTICE_ID}.json").exists()


def test_rejects_task_byte_drift(tmp_path: Path) -> None:
    args = bound_args(tmp_path)
    (args.root / args.task).write_text(task_text(("changed",)), encoding="utf-8")
    with pytest.raises(subject.AckError, match="task bytes changed"):
        subject.run(args)


def test_rejects_todo_and_duplicate_owner_drift(tmp_path: Path) -> None:
    args = bound_args(tmp_path)
    (args.root / "TODO.md").write_text("other.md pb:9\n", encoding="utf-8")
    with pytest.raises(subject.AckError, match="TODO bytes changed"):
        subject.run(args)
    args = bound_args(tmp_path / "previous")
    todo = args.root / "TODO.md"
    todo.write_text(f"current:\n\nprevious:\n{subject.TASK_REF} pb:9\n", encoding="utf-8")
    args = replace(args, todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest())
    with pytest.raises(subject.AckError, match="current TODO row"):
        subject.run(args)
    args = bound_args(tmp_path / "duplicate-row")
    todo = args.root / "TODO.md"
    todo.write_text(f"current:\n{subject.TASK_REF} pb:9\n{subject.TASK_REF} pb:9\n\nprevious:\n", encoding="utf-8")
    args = replace(args, todo_sha256=hashlib.sha256(todo.read_bytes()).hexdigest())
    with pytest.raises(subject.AckError, match="current TODO row"):
        subject.run(args)
    args = bound_args(tmp_path / "duplicate")
    (args.root / "duplicate.md").write_text(task_text(), encoding="utf-8")
    with pytest.raises(subject.AckError, match="exactly one active task owner"):
        subject.run(args)


@pytest.mark.parametrize(
    "update",
    (
        "UPDATE items SET agent_status='collected' WHERE item_id='pb_d66c039216bcbb8ac06b0454'",
        "UPDATE items SET agent_decision='urgent' WHERE item_id='pb_c7ef054f54a31ecad589cc9f'",
        "DELETE FROM items WHERE item_id='pb_b66da04ea53c030da926fb0d'",
        "UPDATE events SET detail_json='{}' WHERE event_id='pb_handoff_2026-09-02T01:37:58+00:00_7e6094f6391c199f'",
    ),
)
def test_rejects_candidate_decision_missing_and_handoff_drift(tmp_path: Path, update: str) -> None:
    args = bound_args(tmp_path)
    connection = sqlite3.connect(args.database)
    connection.execute(update)
    connection.commit()
    connection.close()
    with pytest.raises(subject.AckError):
        subject.run(args)


def test_rejects_database_identity_and_incompatible_receipt(tmp_path: Path) -> None:
    args = bound_args(tmp_path)
    with pytest.raises(subject.AckError, match="database identity changed"):
        subject.run(replace(args, database_ino=args.database_ino + 1))
    args.state_root.mkdir(mode=0o700)
    receipt = args.state_root / f"{subject.NOTICE_ID}.json"
    receipt.write_text("foreign", encoding="utf-8")
    receipt.chmod(0o600)
    with pytest.raises(subject.AckError, match="incompatible"):
        subject.run(args)


def test_rejects_nonterminal_or_missing_report(tmp_path: Path) -> None:
    args = bound_args(tmp_path)
    connection = sqlite3.connect(args.database)
    connection.execute("UPDATE manager_reports SET delivery_status='queued' WHERE report_id=?", (subject.REPORT_IDS[0],))
    connection.commit()
    connection.close()
    with pytest.raises(subject.AckError, match="not terminally closed"):
        subject.run(args)
    args = bound_args(tmp_path / "missing")
    connection = sqlite3.connect(args.database)
    connection.execute("DELETE FROM manager_reports WHERE report_id=?", (subject.REPORT_IDS[0],))
    connection.commit()
    connection.close()
    with pytest.raises(subject.AckError, match="not unique"):
        subject.run(args)


def test_rejects_unsafe_state_directory(tmp_path: Path) -> None:
    args = bound_args(tmp_path)
    args.state_root.mkdir(mode=0o755)
    with pytest.raises(subject.AckError, match="state directory is unsafe"):
        subject.run(args)


@pytest.mark.parametrize("kind", ("symlink", "mode"))
def test_rejects_unsafe_existing_receipt(tmp_path: Path, kind: str) -> None:
    args = bound_args(tmp_path)
    args.state_root.mkdir(mode=0o700)
    receipt = args.state_root / f"{subject.NOTICE_ID}.json"
    if kind == "symlink":
        target = tmp_path / "foreign"
        target.write_text("foreign", encoding="utf-8")
        receipt.symlink_to(target)
    else:
        receipt.write_text("foreign", encoding="utf-8")
        receipt.chmod(0o644)
    with pytest.raises(subject.AckError, match="receipt is unsafe"):
        subject.run(args)


def test_database_writer_is_excluded_while_receipt_is_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = bound_args(tmp_path)
    original = subject.publish_receipt

    def checked_publish(path: Path, payload: bytes) -> str:
        competing = sqlite3.connect(args.database, timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("BEGIN IMMEDIATE")
        finally:
            competing.close()
        return original(path, payload)

    monkeypatch.setattr(subject, "publish_receipt", checked_publish)
    assert subject.run(args) == "acknowledged"


def test_database_connection_is_descriptor_bound_and_path_replacement_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = bound_args(tmp_path)
    original_connect = subject.sqlite3.connect
    replacement = tmp_path / "replacement.sqlite"
    create_database(replacement)
    observed_uri = ""

    def replacing_connect(database: object, *positional: object, **keywords: object) -> sqlite3.Connection:
        nonlocal observed_uri
        observed_uri = str(database)
        args.database.replace(tmp_path / "original-away.sqlite")
        replacement.replace(args.database)
        return original_connect(database, *positional, **keywords)

    monkeypatch.setattr(subject.sqlite3, "connect", replacing_connect)
    with pytest.raises(subject.AckError, match="database changed during acknowledgment"):
        subject.run(args)
    assert "/proc/self/fd/" in observed_uri
