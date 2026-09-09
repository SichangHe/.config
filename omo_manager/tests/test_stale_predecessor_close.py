from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from omo_manager.omo_repository_custody import canonical_json
from omo_manager.omo_stale_predecessor_close import (
    AUDIT_KEYS,
    OPERATION,
    SCHEMA,
    SOURCE1485_PACKET_KEYS,
    SOURCE1485_SCHEMA,
    SOURCE1485_SUCCESSOR_BLOCKER,
    PanePin,
    audit_authorizes,
    audit_authorizes_after_close,
    bound_receipt_id,
    execute,
    live_evidence,
    packet_bytes,
    parse_pin,
    predecessor_snapshots,
    reconstruct_predecessor,
    sha256,
    source1485_live_custody,
    validate_source1485_manager_transition,
    validate_inputs,
    validate_ready_predecessor,
    validate_packet,
    validate_successor,
)
from omo_manager.omo_task_metadata import TaskFrontmatterError


def task_text(body: str = "first\n(done)\nnew work\n") -> bytes:
    return (f"---\nversion: v1.0.0\nstatus: running\nrunat: dw8:1\ntool: codex\nmanagerat: dw:0\nis_manager: false\npending_task_items:\n  - follow up\n---\n{body}").encode()


def manager_text() -> str:
    return "---\nversion: v1.0.0\nstatus: running\nrunat: dw:0\ntool: codex\nmanagerat: wl:1\nis_manager: true\npending_task_items: []\n---\n"


def source1485_successor_text(*, blocker: str = SOURCE1485_SUCCESSOR_BLOCKER, queue: str = "[]", status: str = "blocked") -> bytes:
    blocked_on = f"blocked_on: {blocker}\n" if blocker else ""
    return (f"---\nversion: v1.0.0\nstatus: {status}\n{blocked_on}runat: dw8:1\ntool: codex\nmanagerat: dw:15\nis_manager: false\npending_task_items: {queue}\n---\ncompleted\n").encode()


def source1485_audit(tmp: Path) -> tuple[Path, str]:
    successor = b"authenticated successor manager\n"
    record = {
        "state": "committed",
        "operation": "manager-replace",
        "root": str(tmp),
        "old_task": "dw_manager.md",
        "old_target": "dw:0",
        "successor_task": "dw_root_new.md",
        "new_target": "dw:15",
        "parent_target": "config:1",
        "source1485_topology": {
            "root_task": "dw_root_new.md",
            "root_target": "dw:15.0",
            "parent_target": "config:1.0",
            "rows": [
                {
                    "task": "dw_root_new.md",
                    "is_manager": True,
                    "tool": "codex",
                    "runat": "dw:15.0",
                    "managerat": "config:1.0",
                    "sha256": sha256(successor),
                }
            ],
        },
        "files": [{"task": "dw_root_new.md", "before": None, "after": base64.b64encode(successor).decode()}],
    }
    data = canonical_json(record)
    path = tmp / "source1485-audit.json"
    path.write_bytes(data)
    path.chmod(0o600)
    return path, sha256(data)


def source1485_record(tmp: Path) -> tuple[dict[str, object], str]:
    audit, audit_sha = source1485_audit(tmp)
    record = packet_record(tmp)
    record.update(
        {
            "schema": SOURCE1485_SCHEMA,
            "manager_task": str(tmp / "dw_root_new.md"),
            "manager_target": "dw:15",
            "historical_manager_task": str(tmp / "dw_manager.md"),
            "historical_manager_target": "dw:0",
            "source1485_root_audit": str(audit),
            "source1485_root_audit_sha256": audit_sha,
        }
    )
    return record, audit_sha


def packet_record(tmp: Path) -> dict[str, object]:
    running = ("---\nversion: v1.0.0\nstatus: running\nrunat: dw8:0\ntool: codex\nmanagerat: dw:0\nis_manager: false\npending_task_items: []\n---\nfirst\n(done)\n").encode()
    done = running.replace(b"status: running\n", b"status: done\n")
    secret = "a" * 64
    helper = Path(__file__).parents[1] / "omo_stale_predecessor_close.py"
    record: dict[str, object] = {
        "schema": SCHEMA,
        "root": str(tmp),
        "task": str(tmp / "worker.md"),
        "todo": str(tmp / "TODO.md"),
        "manager_task": str(tmp / "manager.md"),
        "manager_target": "dw:0",
        "predecessor_target": "dw8:0",
        "predecessor_pane": {"target": "dw8:0", "pane_id": "%1", "pane_pid": 101, "pane_start_ticks": 201},
        "predecessor_session_id": "01a07f0f-ffbd-7f13-89f1-4936c50be5c2",
        "protected_target": "dw8:1",
        "protected_pane": {"target": "dw8:1", "pane_id": "%2", "pane_pid": 102, "pane_start_ticks": 202},
        "protected_session_id": "01a07faa-011d-7983-86bb-978cf5a25169",
        "task_sha256": "b" * 64,
        "todo_sha256": "c" * 64,
        "manager_task_sha256": "d" * 64,
        "consumed_export": str(tmp / "consumed.json"),
        "consumed_export_sha256": "e" * 64,
        "predecessor_running_base64": base64.b64encode(running).decode(),
        "predecessor_running_sha256": sha256(running),
        "predecessor_done_base64": base64.b64encode(done).decode(),
        "predecessor_done_sha256": sha256(done),
        "report_replay_id": "1" * 64,
        "report_attestation_id": "2" * 64,
        "helper": str(helper),
        "helper_sha256": sha256(helper.read_bytes()),
        "close_proof_secret": secret,
        "close_proof_commitment": sha256(secret.encode()),
        "audit": str(tmp / "audit.json"),
        "inputs": [],
    }
    return record


class StalePredecessorCloseTests(unittest.TestCase):
    def test_reconstructs_unique_predecessor_prefix(self) -> None:
        root = Path("/tmp/work-logs")
        active = task_text()
        header, body = predecessor_snapshots(active, root, "dw8:0")
        running = header + body[: len(b"first\n(done)\n")]
        done = running.replace(b"status: running\n", b"status: done\n")
        self.assertEqual(
            (running, done),
            reconstruct_predecessor(active, root, "dw8:0", sha256(running), len(running), sha256(done), len(done)),
        )

    def test_reconstruction_rejects_unpreserved_body(self) -> None:
        active = task_text("changed\n")
        with self.assertRaisesRegex(TaskFrontmatterError, "preserve one exact"):
            reconstruct_predecessor(active, Path("/tmp/work-logs"), "dw8:0", "0" * 64, 100, "1" * 64, 97)

    def test_packet_round_trip_binds_secret_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = packet_record(Path(directory))
            data = packet_bytes(record)
            parsed = validate_packet(data, sha256(data))
            self.assertEqual(SCHEMA, parsed["schema"])
            self.assertEqual(bound_receipt_id({key: value for key, value in parsed.items() if key != "binding_id"}), parsed["binding_id"])

    def test_source1485_packet_authenticates_historical_to_current_manager_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            record, audit_sha = source1485_record(tmp)
            data = packet_bytes(record)
            with (
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_SHA256", audit_sha),
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_PATH", tmp / "source1485-audit.json"),
            ):
                parsed = validate_packet(data, sha256(data))
                historical, target, audit = validate_source1485_manager_transition(parsed)
            self.assertEqual(SOURCE1485_PACKET_KEYS, set(parsed))
            self.assertEqual((tmp / "dw_manager.md", "dw:0", tmp / "source1485-audit.json"), (historical, target, audit))

    def test_source1485_transition_rejects_wrong_old_or_current_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            original, audit_sha = source1485_record(tmp)
            for field, value in (
                ("historical_manager_task", str(tmp / "other-old.md")),
                ("historical_manager_target", "dw:9"),
                ("manager_task", str(tmp / "other-current.md")),
                ("manager_target", "dw:9"),
            ):
                with self.subTest(field=field):
                    record = dict(original)
                    record[field] = value
                    with (
                        patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_SHA256", audit_sha),
                        patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_PATH", tmp / "source1485-audit.json"),
                        self.assertRaisesRegex(TaskFrontmatterError, "transition evidence"),
                    ):
                        validate_source1485_manager_transition(record)

    def test_source1485_transition_rejects_wrong_audit_path_digest_and_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            original, audit_sha = source1485_record(tmp)
            wrong_path = dict(original)
            wrong_path["source1485_root_audit"] = "relative.json"
            wrong_digest = dict(original)
            wrong_digest["source1485_root_audit_sha256"] = "0" * 64
            for record, message in ((wrong_path, "binding"), (wrong_digest, "binding")):
                with (
                    patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_SHA256", audit_sha),
                    patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_PATH", tmp / "source1485-audit.json"),
                    self.assertRaisesRegex(TaskFrontmatterError, message),
                ):
                    validate_source1485_manager_transition(record)
            (tmp / "source1485-audit.json").write_bytes(b"{}\n")
            with (
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_SHA256", audit_sha),
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_PATH", tmp / "source1485-audit.json"),
                self.assertRaisesRegex(TaskFrontmatterError, "changed"),
            ):
                validate_source1485_manager_transition(original)

    def test_source1485_transition_rejects_semantic_audit_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            original, _audit_sha = source1485_record(tmp)
            audit_path = tmp / "source1485-audit.json"
            audit = json.loads(audit_path.read_bytes())
            audit["old_target"] = "dw:9"
            data = canonical_json(audit)
            audit_path.write_bytes(data)
            semantic_sha = sha256(data)
            original["source1485_root_audit_sha256"] = semantic_sha
            with (
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_SHA256", semantic_sha),
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_PATH", tmp / "source1485-audit.json"),
                self.assertRaisesRegex(TaskFrontmatterError, "transition evidence"),
            ):
                validate_source1485_manager_transition(original)

    def test_source1485_live_evidence_uses_historical_consumed_manager_and_current_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            record, audit_sha = source1485_record(tmp)
            record["inputs"] = [{}] * 6
            running = base64.b64decode(cast(str, record["predecessor_running_base64"]))
            done = base64.b64decode(cast(str, record["predecessor_done_base64"]))
            consumed = (
                {"replay_id": record["report_replay_id"], "attestation_id": record["report_attestation_id"]},
                running,
                done,
                tmp / "dw_manager.md",
                (),
            )
            current_manager = SimpleNamespace(is_manager=True, runat="dw:15", status="running")
            with (
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_SHA256", audit_sha),
                patch("omo_manager.omo_stale_predecessor_close.SOURCE1485_ROOT_AUDIT_PATH", tmp / "source1485-audit.json"),
                patch("omo_manager.omo_stale_predecessor_close.read_private_or_owned", return_value=b"current bytes"),
                patch("omo_manager.omo_stale_predecessor_close.validate_successor"),
                patch("omo_manager.omo_stale_predecessor_close.parse_task_metadata", return_value=current_manager),
                patch("omo_manager.omo_stale_predecessor_close.validate_consumed_export", return_value=consumed) as validate_consumed,
                patch("omo_manager.omo_stale_predecessor_close.file_input", return_value={}),
                patch(
                    "omo_manager.omo_stale_predecessor_close.authoritative_active_target_task_paths",
                    side_effect=lambda _root, target: (tmp / "dw_root_new.md",) if target == "dw:15" else (),
                ),
                patch("omo_manager.omo_stale_predecessor_close.current_pin", return_value=True),
                patch("omo_manager.omo_stale_predecessor_close.codex_status", return_value="ready"),
            ):
                live_evidence(record, query_sessions=False)
            self.assertEqual("dw:0", validate_consumed.call_args.args[-1])

    def test_only_source1485_accepts_exact_completed_blocked_successor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            task = tmp / "worker.md"
            todo = b"worker.md dw8:1\n"
            task.write_bytes(source1485_successor_text())
            with (
                patch("omo_manager.omo_stale_predecessor_close.current_target_task_paths", return_value=(task,)),
                patch("omo_manager.omo_stale_predecessor_close.authoritative_active_target_task_paths", return_value=(task,)),
            ):
                validate_successor(task, task.read_bytes(), todo, tmp, "dw8:1", "dw:15", source1485=True)
                with self.assertRaisesRegex(TaskFrontmatterError, "lifecycle identity"):
                    validate_successor(task, task.read_bytes(), todo, tmp, "dw8:1", "dw:15")
            header, _body = predecessor_snapshots(task.read_bytes(), tmp, "dw8:0", source1485=True)
            self.assertIn(b"managerat: dw:0\n", header)
            with self.assertRaisesRegex(TaskFrontmatterError, "active non-manager"):
                predecessor_snapshots(task.read_bytes(), tmp, "dw8:0")

    def test_source1485_rejects_other_blocked_states_and_noncurrent_todo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            task = tmp / "worker.md"
            todo = b"worker.md dw8:1\n"
            cases = (
                (source1485_successor_text(blocker="other.md: waiting"), (task,)),
                (source1485_successor_text(queue="\n  - remaining work"), (task,)),
                (source1485_successor_text(status="running", blocker=""), (task,)),
                (source1485_successor_text(), ()),
            )
            for data, current in cases:
                with (
                    self.subTest(data=data, current=current),
                    patch("omo_manager.omo_stale_predecessor_close.current_target_task_paths", return_value=current),
                    patch("omo_manager.omo_stale_predecessor_close.authoritative_active_target_task_paths", return_value=(task,)),
                    self.assertRaisesRegex(TaskFrontmatterError, "lifecycle identity"),
                ):
                    validate_successor(task, data, todo, tmp, "dw8:1", "dw:15", source1485=True)

    def test_source1485_live_custody_requires_ready_pane_and_singular_current_manager(self) -> None:
        root = Path("/tmp/work-logs")
        manager = root / "dw_root_new.md"
        for status, owners in (("running", (manager,)), ("ready", ()), ("ready", (manager, root / "other.md"))):
            with (
                self.subTest(status=status, owners=owners),
                patch("omo_manager.omo_stale_predecessor_close.codex_status", return_value=status),
                patch("omo_manager.omo_stale_predecessor_close.authoritative_active_target_task_paths", return_value=owners),
            ):
                self.assertFalse(source1485_live_custody(root, "dw8:1", "dw:15", manager))
        with (
            patch("omo_manager.omo_stale_predecessor_close.codex_status", return_value="ready"),
            patch("omo_manager.omo_stale_predecessor_close.authoritative_active_target_task_paths", return_value=(manager,)),
        ):
            self.assertTrue(source1485_live_custody(root, "dw8:1", "dw:15", manager))

    def test_packet_rejects_target_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = packet_record(Path(directory))
            record["protected_target"] = "dw8:0"
            protected = dict(cast(dict[str, object], record["protected_pane"]))
            protected["target"] = "dw8:0"
            record["protected_pane"] = protected
            data = packet_bytes(record)
            with self.assertRaisesRegex(TaskFrontmatterError, "target scope"):
                validate_packet(data, sha256(data))

    def test_packet_rejects_image_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = packet_record(Path(directory))
            record["predecessor_done_base64"] = base64.b64encode(b"forged").decode()
            data = packet_bytes(record)
            with self.assertRaisesRegex(TaskFrontmatterError, "done image"):
                validate_packet(data, sha256(data))

    def test_parse_pin_rejects_boolean_pid(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "process identity"):
            parse_pin({"target": "dw8:0", "pane_id": "%1", "pane_pid": True, "pane_start_ticks": 2}, "pane")

    def test_input_set_rejects_omission_and_replacement(self) -> None:
        expected: list[dict[str, object]] = [
            {"file": {"path": "/tmp/a"}, "ancestors": []},
            {"file": {"path": "/tmp/b"}, "ancestors": []},
        ]
        validate_inputs(expected, expected)
        for recorded in (expected[:-1], [expected[0], {"file": {"path": "/tmp/c"}, "ancestors": []}]):
            with self.assertRaisesRegex(TaskFrontmatterError, "exact complete input set"):
                validate_inputs(recorded, expected)

    def test_child_audit_requires_unchanged_protected_files_and_pane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            task, todo = tmp / "worker.md", tmp / "TODO.md"
            task.write_bytes(task_text())
            todo.write_text("worker.md dw8:1\n")
            (tmp / "manager.md").write_text(manager_text())
            record = packet_record(tmp)
            record = validate_packet(packet_bytes(record))
            audit = {key: record[key] for key in AUDIT_KEYS if key in record}
            audit.update({"schema": SCHEMA, "operation": OPERATION, "state": "prepared", "packet_sha256": "3" * 64})
            audit["task_sha256"] = hashlib.sha256(task.read_bytes()).hexdigest()
            audit["todo_sha256"] = hashlib.sha256(todo.read_bytes()).hexdigest()
            audit["manager_task_sha256"] = hashlib.sha256((tmp / "manager.md").read_bytes()).hexdigest()
            text = canonical_json(audit).decode()
            predecessor = PanePin("dw8:0", "%1", 101, 201)
            with (
                patch("omo_manager.omo_stale_predecessor_close.current_pin", return_value=True),
                patch("omo_manager.omo_stale_predecessor_close.codex_status", return_value="ready"),
                patch("omo_manager.omo_stale_predecessor_close.pinned_current_command", return_value="zsh"),
                patch("omo_manager.omo_stale_predecessor_close.session_from_process", side_effect=lambda pin: record[f"{'predecessor' if pin.target == 'dw8:0' else 'protected'}_session_id"]),
                patch(
                    "omo_manager.omo_stale_predecessor_close.validate_consumed_export",
                    return_value=(
                        {"replay_id": record["report_replay_id"], "attestation_id": record["report_attestation_id"]},
                        base64.b64decode(cast(str, record["predecessor_running_base64"])),
                        base64.b64decode(cast(str, record["predecessor_done_base64"])),
                        tmp / "manager.md",
                        (),
                    ),
                ),
                patch("omo_manager.omo_stale_predecessor_close.authoritative_active_target_task_paths", side_effect=lambda _root, target: (task,) if target == "dw8:1" else ()),
                patch("omo_manager.omo_stale_predecessor_close.file_input", return_value={}),
            ):
                audit["inputs"] = [{}] * 5
                text = canonical_json(audit).decode()
                self.assertTrue(
                    audit_authorizes(
                        text,
                        audit,
                        str(record["close_proof_commitment"]),
                        predecessor.target,
                        predecessor.pane_id,
                        predecessor.pane_pid,
                        predecessor.pane_start_ticks,
                        tmp / "audit.json.prepared",
                    )
                )
                with (
                    patch("omo_manager.omo_stale_predecessor_close.exact_pane_id", return_value=""),
                    patch("omo_manager.omo_stale_predecessor_close.resolve_pane_id", return_value=""),
                    patch("omo_manager.omo_stale_predecessor_close.process_start_ticks", return_value=None),
                ):
                    self.assertTrue(
                        audit_authorizes_after_close(
                            text,
                            audit,
                            str(record["close_proof_commitment"]),
                            predecessor.target,
                            predecessor.pane_id,
                            predecessor.pane_pid,
                            predecessor.pane_start_ticks,
                            tmp / "audit.json.prepared",
                        )
                    )
            task.write_text("drift\n")
            with patch("omo_manager.omo_stale_predecessor_close.current_pin", return_value=True):
                self.assertFalse(
                    audit_authorizes(
                        text,
                        audit,
                        str(record["close_proof_commitment"]),
                        predecessor.target,
                        predecessor.pane_id,
                        predecessor.pane_pid,
                        predecessor.pane_start_ticks,
                        tmp / "audit.json.prepared",
                    )
                )

    def test_predecessor_ready_check_rejects_resumed_work(self) -> None:
        predecessor = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch("omo_manager.omo_stale_predecessor_close.current_pin", return_value=True),
            patch("omo_manager.omo_stale_predecessor_close.codex_status", return_value="running"),
            self.assertRaisesRegex(TaskFrontmatterError, "resumed or changed"),
        ):
            validate_ready_predecessor(predecessor)

    def test_child_audit_rejects_extra_field(self) -> None:
        record: dict[str, object] = {key: "" for key in AUDIT_KEYS}
        record["extra"] = True
        self.assertFalse(audit_authorizes(json.dumps(record), record, "0" * 64, "dw8:0", "%1", 1, 1, Path("/tmp/audit.prepared")))

    def test_execute_recovers_exact_shell_after_exit_before_started_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            record = packet_record(tmp)
            predecessor = PanePin("dw8:0", "%1", 101, 201)
            protected = PanePin("dw8:1", "%2", 102, 202)
            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="f" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="e" * 64,
            )
            with (
                patch("omo_manager.omo_stale_predecessor_close.read_private", return_value=b"packet"),
                patch("omo_manager.omo_stale_predecessor_close.validate_packet", return_value=record),
                patch("omo_manager.omo_stale_predecessor_close.validate_review"),
                patch("omo_manager.omo_stale_predecessor_close.task_target_lock", side_effect=lambda *_args: contextlib.nullcontext()),
                patch("omo_manager.omo_stale_predecessor_close.task_file_lock", side_effect=lambda *_args: contextlib.nullcontext()),
                patch("omo_manager.omo_stale_predecessor_close.pin_is_absent", return_value=False),
                patch("omo_manager.omo_stale_predecessor_close.path_entry_exists", return_value=True),
                patch("omo_manager.omo_stale_predecessor_close.current_pin", return_value=True),
                patch("omo_manager.omo_stale_predecessor_close.pinned_current_command", return_value="zsh"),
                patch("omo_manager.omo_stale_predecessor_close.live_evidence", return_value=(predecessor, protected)) as evidence,
                patch("omo_manager.omo_stale_predecessor_close.prepared_audit", return_value=b"{}\n"),
                patch("omo_manager.omo_stale_predecessor_close.publish_or_validate") as publish,
                patch("omo_manager.omo_stale_predecessor_close.has_bound_close_proof", side_effect=[False, True]),
                patch("omo_manager.omo_stale_predecessor_close.exact_pane_id", side_effect=["%1", "%1", ""]),
                patch("omo_manager.omo_stale_predecessor_close.session_from_process", return_value=record["protected_session_id"]),
                patch("omo_manager.omo_stale_predecessor_close.process_start_ticks", return_value=None),
                patch("omo_manager.omo_stale_predecessor_close.close_bound_tmux_target") as close_shell,
                patch("omo_manager.omo_stale_predecessor_close.guarded_codex_stop") as stop_codex,
            ):
                execute(args)
            evidence.assert_called_once_with(record, predecessor_absent=False, predecessor_shell=True)
            stop_codex.assert_not_called()
            close_shell.assert_called_once()
            self.assertEqual(OPERATION, close_shell.call_args.args[-2])
            self.assertEqual(2, publish.call_count)


if __name__ == "__main__":
    unittest.main()
