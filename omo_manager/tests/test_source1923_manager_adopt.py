from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_codex_stop, omo_source1923_manager_adopt as adopt
from omo_manager.omo_repository_custody import CustodyError
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata


SOURCE_SESSION = "11111111-1111-4111-8111-111111111111"
SUCCESSOR_SESSION = "22222222-2222-4222-8222-222222222222"
EXECUTOR_SESSION = "33333333-3333-4333-8333-333333333333"
CHILD_SESSION = "55555555-5555-4555-8555-555555555555"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_text(
    *,
    status: str,
    runat: str,
    managerat: str,
    is_manager: bool,
    pending: tuple[str, ...] = (),
    blocked_on: str = "",
    body: str = "task body",
) -> str:
    blocker = f"blocked_on: {blocked_on}\n" if blocked_on else ""
    queue = "pending_task_items: []\n" if not pending else "pending_task_items:\n" + "".join(f"  - {json.dumps(item)}\n" for item in pending)
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocker}runat: {runat}\ntool: codex\nmanagerat: {managerat}\nis_manager: {'true' if is_manager else 'false'}\n{queue}---\n{body}\n"


class Source1923AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "work_logs"
        self.root.mkdir(mode=0o700)
        (self.root / "manager_mail").mkdir()
        (self.root / "202608").mkdir()
        self.private = Path(self.temporary.name) / "private"
        self.private.mkdir(mode=0o700)
        self.packet = self.private / "packet.json"
        self.audit = self.private / "audit.json"
        self.review = self.private / "review.json"

        self.authority = self.root / adopt.AUTHORITY_REF
        self.authority.write_text(adopt.AUTHORITY_TEXT + "\n", encoding="utf-8")
        self.authority.chmod(0o600)
        self.authority_sha = digest(self.authority.read_bytes())
        self.envelope = self.root / adopt.AUTHORITY_ENVELOPE_TASK
        self.envelope.write_text(
            f'<human_instruction authoritative="true" source="{adopt.AUTHORITY_REF}:1-10">\n{adopt.AUTHORITY_TEXT}\n</human_instruction>\n',
            encoding="utf-8",
        )

        self.source_queue = ("old work one", "old work two")
        self.successor_queue = ("Source-1847 protected work", "Source-1922 cleanup")
        self.source = task_text(
            status="blocked",
            blocked_on=adopt.SOURCE_BLOCKER,
            runat=adopt.SOURCE_TARGET,
            managerat=adopt.PARENT_TARGET,
            is_manager=True,
            pending=self.source_queue,
        )
        self.successor = task_text(
            status="long_running",
            blocked_on="persistent manager role",
            runat=adopt.SUCCESSOR_TARGET,
            managerat=adopt.SOURCE_TARGET,
            is_manager=True,
            pending=self.successor_queue,
        )
        self.historical = task_text(
            status="blocked",
            blocked_on=adopt.HISTORICAL_BLOCKER,
            runat=adopt.SUCCESSOR_TARGET,
            managerat=adopt.HISTORICAL_MANAGER,
            is_manager=True,
            body="Human hold remains here.\n\n(manager closed Codex agent 09-12 09:43 PDT; tmux target `dw:33`; Codex session id old.)",
        )
        self.old_child = task_text(status="blocked", blocked_on="retained hold", runat="dw:36", managerat=adopt.SOURCE_TARGET, is_manager=True)
        self.source1847 = task_text(
            status="running",
            runat="cc-through-july-2026:1",
            managerat=adopt.SUCCESSOR_TARGET,
            is_manager=False,
            pending=("Source-1847 exact queue",),
        )
        self.write(adopt.SOURCE_TASK, self.source)
        self.write(adopt.SUCCESSOR_TASK, self.successor)
        self.write(adopt.HISTORICAL_TASK, self.historical)
        self.write("old_child.md", self.old_child)
        self.write("source1847_cc_rebuild.md", self.source1847)
        self.todo = (
            "current:\n"
            f"{adopt.SOURCE_TASK} {adopt.SOURCE_TARGET}\n"
            f"{adopt.SUCCESSOR_TASK} {adopt.SUCCESSOR_TARGET}\n"
            "old_child.md dw:36\n"
            "source1847_cc_rebuild.md cc-through-july-2026:1\n"
            "\nhuman pending:\n\nlow priority:\n\nprevious:\n"
        )
        self.write("TODO.md", self.todo)
        self.archive = "archived from TODO.md previous on 2026-09-12:\nold.md old:1\ndw_recon_live_mgr.md dw:33\n"
        self.write(adopt.ARCHIVE_INDEX, self.archive)

        self.root_patch = patch.dict(
            os.environ,
            {
                adopt.TRUSTED_ROOT_ENV: str(self.root),
                adopt.EXECUTOR_TARGET_ENV: "config:24",
                adopt.EXECUTOR_SESSION_ENV: EXECUTOR_SESSION,
            },
        )
        self.root_device_patch = patch.object(adopt, "TRUSTED_ROOT_DEVICE", self.root.stat().st_dev)
        self.root_inode_patch = patch.object(adopt, "TRUSTED_ROOT_INODE", self.root.stat().st_ino)
        self.authority_patch = patch.object(adopt, "AUTHORITY_SHA256", self.authority_sha)
        self.historical_patch = patch.object(adopt, "HISTORICAL_SHA256", digest(self.historical.encode()))
        self.root_patch.start()
        self.root_device_patch.start()
        self.root_inode_patch.start()
        self.authority_patch.start()
        self.historical_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.root_device_patch.stop)
        self.addCleanup(self.root_inode_patch.stop)
        self.addCleanup(self.authority_patch.stop)
        self.addCleanup(self.historical_patch.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def pane(self, target: str, number: int) -> adopt.PanePin:
        return adopt.PanePin(target, f"%{number}", 1000 + number, 2000 + number)

    def task_pins(self) -> list[adopt.TaskPin]:
        records, _, _ = adopt.tree_records(self.root)
        return [adopt.TaskPin(str(item["task"]), str(item["sha256"])) for item in records]

    def args(self, **changes: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "root": self.root,
            "source_task": Path(adopt.SOURCE_TASK),
            "successor_task": Path(adopt.SUCCESSOR_TASK),
            "historical_task": Path(adopt.HISTORICAL_TASK),
            "source_target": adopt.SOURCE_TARGET,
            "successor_target": adopt.SUCCESSOR_TARGET,
            "parent_target": adopt.PARENT_TARGET,
            "executor_target": "config:24",
            "source_sha256": digest((self.root / adopt.SOURCE_TASK).read_bytes()),
            "successor_sha256": digest((self.root / adopt.SUCCESSOR_TASK).read_bytes()),
            "historical_sha256": digest((self.root / adopt.HISTORICAL_TASK).read_bytes()),
            "todo_sha256": digest((self.root / "TODO.md").read_bytes()),
            "archive_index_sha256": digest((self.root / adopt.ARCHIVE_INDEX).read_bytes()),
            "authority": self.authority,
            "authority_sha256": self.authority_sha,
            "authority_lines": adopt.AUTHORITY_LINES,
            "authority_envelope": self.envelope,
            "authority_envelope_sha256": digest(self.envelope.read_bytes()),
            "source_pin": self.pane(adopt.SOURCE_TARGET, 1),
            "successor_pin": self.pane(adopt.SUCCESSOR_TARGET, 2),
            "executor_pin": self.pane("config:24", 3),
            "node": self.task_pins(),
            "live_node": [],
            "packet": self.packet,
            "audit": self.audit,
            "preparer": "config:24",
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def exact_pane(self, target: str) -> str:
        return {adopt.SOURCE_TARGET: "%1", adopt.SUCCESSOR_TARGET: "%2", "config:24": "%3"}.get(target, "")

    def session(self, pin: adopt.PanePin, _protected: tuple[adopt.PanePin, ...], *, may_query: bool) -> str:
        del may_query
        if pin.target == adopt.SOURCE_TARGET:
            return SOURCE_SESSION
        return SUCCESSOR_SESSION if pin.target == adopt.SUCCESSOR_TARGET else EXECUTOR_SESSION

    def prepare(self) -> dict[str, object]:
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
        ):
            adopt.prepare(self.args())
        return json.loads(self.packet.read_text())

    def write_review(self) -> str:
        packet_sha = digest(self.packet.read_bytes())
        record = {
            "schema": adopt.REVIEW_SCHEMA,
            "verdict": "PASS",
            "packet_sha256": packet_sha,
            "helper_sha256": adopt.helper_sha256(),
            "reviewer": "independent-reviewer",
        }
        self.review.write_bytes(adopt.canonical_json(record))
        self.review.chmod(0o600)
        return packet_sha

    def execute_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            packet=self.packet,
            packet_sha256=digest(self.packet.read_bytes()),
            review=self.review,
            review_sha256=digest(self.review.read_bytes()),
        )

    def test_success_adopts_existing_successor_and_preserves_queues_and_hold(self) -> None:
        packet = self.prepare()
        _ = self.write_review()
        stopped = {"value": False}

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
        ):
            adopt.execute(self.execute_args())
            adopt.execute(self.execute_args())

        source = parse_task_metadata((self.root / adopt.SOURCE_TASK).read_text(), self.root)
        successor = parse_task_metadata((self.root / adopt.SUCCESSOR_TASK).read_text(), self.root)
        historical = parse_task_metadata((self.root / adopt.HISTORICAL_TASK).read_text(), self.root)
        old_child = parse_task_metadata((self.root / "old_child.md").read_text(), self.root)
        source1847 = (self.root / "source1847_cc_rebuild.md").read_text()
        self.assertIsNotNone(source)
        self.assertIsNotNone(successor)
        self.assertIsNotNone(historical)
        self.assertIsNotNone(old_child)
        assert source is not None and successor is not None and historical is not None and old_child is not None
        self.assertEqual("done", source.status)
        self.assertEqual((), source.pending_task_items)
        self.assertEqual(adopt.PARENT_TARGET, successor.managerat)
        self.assertEqual((adopt.SOURCE1923_ITEM, *self.successor_queue, *self.source_queue), successor.pending_task_items)
        self.assertEqual(adopt.SUCCESSOR_TARGET, old_child.managerat)
        self.assertEqual("blocked", historical.status)
        self.assertEqual("retired", historical.runat)
        self.assertEqual(adopt.HISTORICAL_BLOCKER, historical.blocked_on)
        self.assertIn("Human hold remains here.", (self.root / adopt.HISTORICAL_TASK).read_text())
        self.assertEqual(self.source1847, source1847)
        self.assertIn(f"{adopt.SOURCE_TASK} {adopt.SOURCE_TARGET}", (self.root / "TODO.md").read_text().split("previous:", 1)[1])
        self.assertIn("dw_recon_live_mgr.md retired", (self.root / adopt.ARCHIVE_INDEX).read_text())
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])
        self.assertEqual(packet["final_successor_queue_sha256"], adopt.json_digest(list(successor.pending_task_items)))

    def test_authority_mismatch_writes_nothing(self) -> None:
        before = (self.root / adopt.SOURCE_TASK).read_bytes()
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            self.assertRaisesRegex(TaskFrontmatterError, "exact Source-1923"),
        ):
            adopt.prepare(self.args(authority_sha256="0" * 64))
        self.assertEqual(before, (self.root / adopt.SOURCE_TASK).read_bytes())
        self.assertFalse(self.packet.exists())

    def test_running_session_uses_read_only_process_binding(self) -> None:
        pin = self.pane("dw:4", 4)
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "session_from_process", return_value=CHILD_SESSION),
            patch.object(adopt, "visible_session_id") as visible,
            patch.object(adopt, "query_status_session_id") as query,
        ):
            self.assertEqual(CHILD_SESSION, adopt.live_session_id(pin, (pin,), may_query=False))
        visible.assert_not_called()
        query.assert_not_called()

    def test_running_session_without_process_binding_fails_closed(self) -> None:
        pin = self.pane("dw:4", 4)
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "session_from_process", side_effect=TaskFrontmatterError("unavailable")),
            patch.object(adopt, "visible_session_id", return_value=""),
            patch.object(adopt, "query_status_session_id") as query,
            self.assertRaisesRegex(TaskFrontmatterError, "not uniquely visible"),
        ):
            adopt.live_session_id(pin, (pin,), may_query=False)
        query.assert_not_called()

    def test_process_session_binding_rejects_protected_process_drift(self) -> None:
        pin = self.pane("dw:4", 4)
        stable_checks = iter((True, True, True, False))
        with (
            patch.object(adopt, "current_pin", side_effect=lambda _pin: next(stable_checks)),
            patch.object(adopt, "session_from_process", return_value=CHILD_SESSION),
            self.assertRaisesRegex(TaskFrontmatterError, "could not be authenticated"),
        ):
            adopt.live_session_id(pin, (pin,), may_query=False)

    def test_copied_root_identity_cannot_authorize_prepare(self) -> None:
        copied_root = Path(self.temporary.name) / "copied-work-logs"
        copied_root.mkdir()
        with (
            patch.dict(os.environ, {adopt.TRUSTED_ROOT_ENV: str(copied_root)}),
            self.assertRaisesRegex(TaskFrontmatterError, "exact Source-1923 repository identity"),
        ):
            adopt.prepare(self.args())
        self.assertFalse(self.packet.exists())

    def test_codex_stop_accepts_only_the_exact_prepared_close_audit(self) -> None:
        packet = self.prepare()
        packet_sha = self.write_review()
        review_sha = digest(self.review.read_bytes())
        prepared = adopt.prepared_audit(packet, packet_sha, self.review, review_sha, "independent-reviewer")
        path = self.audit.with_name(f"{self.audit.name}.prepared")
        path.write_bytes(prepared)
        path.chmod(0o600)
        source = packet["source_pane"]
        assert isinstance(source, dict)
        omo_codex_stop.validate_bound_close_audit_file(
            adopt.CLOSE_OPERATION,
            path,
            str(packet["close_proof_commitment"]),
            str(source["target"]),
            str(source["pane_id"]),
            int(source["pane_pid"]),
            int(source["pane_start_ticks"]),
            digest(prepared),
        )
        with self.assertRaisesRegex(RuntimeError, "drifted"):
            omo_codex_stop.validate_bound_close_audit_file(
                adopt.CLOSE_OPERATION,
                path,
                str(packet["close_proof_commitment"]),
                str(source["target"]),
                str(source["pane_id"]),
                int(source["pane_pid"]) + 1,
                int(source["pane_start_ticks"]),
                digest(prepared),
            )

        fabricated_record = json.loads(prepared)
        fabricated_record["review"] = str(self.private / "missing-review.json")
        fabricated = adopt.canonical_json(fabricated_record)
        path.write_bytes(fabricated)
        path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            omo_codex_stop.validate_bound_close_audit_file(
                adopt.CLOSE_OPERATION,
                path,
                str(packet["close_proof_commitment"]),
                str(source["target"]),
                str(source["pane_id"]),
                int(source["pane_pid"]),
                int(source["pane_start_ticks"]),
                digest(fabricated),
            )

    def test_codex_stop_rejects_byte_identical_replaced_input_parent(self) -> None:
        packet = self.prepare()
        packet_sha = self.write_review()
        review_sha = digest(self.review.read_bytes())
        prepared = adopt.prepared_audit(packet, packet_sha, self.review, review_sha, "independent-reviewer")
        path = self.audit.with_name(f"{self.audit.name}.prepared")
        path.write_bytes(prepared)
        path.chmod(0o600)
        original_parent = self.root / "202608"
        retained_parent = self.root / "202608-retained"
        original_parent.rename(retained_parent)
        shutil.copytree(retained_parent, original_parent)
        source = packet["source_pane"]
        assert isinstance(source, dict)

        with self.assertRaisesRegex(RuntimeError, "invalid"):
            omo_codex_stop.validate_bound_close_audit_file(
                adopt.CLOSE_OPERATION,
                path,
                str(packet["close_proof_commitment"]),
                str(source["target"]),
                str(source["pane_id"]),
                int(source["pane_pid"]),
                int(source["pane_start_ticks"]),
                digest(prepared),
            )

    def test_incomplete_mutation_plan_does_not_stop(self) -> None:
        packet = self.prepare()
        files = packet["files"]
        assert isinstance(files, list)
        packet["files"] = [item for item in files if isinstance(item, dict) and item.get("task") != adopt.SOURCE_TASK]
        packet.pop("binding_id")
        packet["binding_id"] = digest(adopt.canonical_json(packet))
        self.packet.write_bytes(adopt.canonical_json(packet))
        self.write_review()
        with patch.object(adopt, "stop_source") as stop, self.assertRaisesRegex(TaskFrontmatterError, "mutation plan"):
            adopt.execute(self.execute_args())
        stop.assert_not_called()

    def test_historical_record_mismatch_writes_nothing(self) -> None:
        path = self.root / adopt.HISTORICAL_TASK
        path.write_text(self.historical.replace("Human hold remains here.", "changed evidence"))
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            self.assertRaisesRegex(TaskFrontmatterError, "historical dw:33"),
        ):
            adopt.prepare(self.args(historical_sha256=digest(path.read_bytes())))
        self.assertFalse(self.packet.exists())

    def test_queue_drift_after_prepare_writes_nothing_and_does_not_stop(self) -> None:
        self.prepare()
        self.write_review()
        path = self.root / "source1847_cc_rebuild.md"
        path.write_text(self.source1847.replace("Source-1847 exact queue", "drifted queue"))
        current = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in json.loads(self.packet.read_text())["files"]}
        with patch.object(adopt, "stop_source") as stop, self.assertRaises(CustodyError):
            adopt.execute(self.execute_args())
        stop.assert_not_called()
        self.assertEqual(current, {task: (self.root / str(task)).read_bytes() for task in current})

    def test_child_set_drift_after_prepare_writes_nothing_and_does_not_stop(self) -> None:
        self.prepare()
        self.write_review()
        self.write("new_child.md", task_text(status="running", runat="dw:77", managerat=adopt.SOURCE_TARGET, is_manager=False))
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source") as stop,
            self.assertRaisesRegex(TaskFrontmatterError, "tree changed"),
        ):
            adopt.execute(self.execute_args())
        stop.assert_not_called()

    def test_direct_child_drift_at_guarded_pre_input_does_not_kill(self) -> None:
        self.prepare()
        self.write_review()
        killed = {"value": False}

        def guarded(args: object) -> str:
            self.write("last_moment_child.md", task_text(status="running", runat="dw:87", managerat=adopt.SOURCE_TARGET, is_manager=False))
            callback = getattr(args, "bound_pre_input_check")
            callback()
            killed["value"] = True
            return SOURCE_SESSION

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "guarded_codex_stop", side_effect=guarded),
            self.assertRaisesRegex(TaskFrontmatterError, "immediately before guarded pane input"),
        ):
            adopt.execute(self.execute_args())
        self.assertFalse(killed["value"])

    def test_process_drift_after_prepare_writes_nothing_and_does_not_stop(self) -> None:
        self.prepare()
        self.write_review()
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in json.loads(self.packet.read_text())["files"]}

        def current(pin: adopt.PanePin) -> bool:
            return pin.target != adopt.SUCCESSOR_TARGET

        with (
            patch.object(adopt, "current_pin", side_effect=current),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "stop_source") as stop,
            self.assertRaisesRegex(TaskFrontmatterError, "successor pane/process changed"),
        ):
            adopt.execute(self.execute_args())
        stop.assert_not_called()
        self.assertEqual(before, {task: (self.root / str(task)).read_bytes() for task in before})

    def test_executor_session_drift_writes_nothing_and_does_not_stop(self) -> None:
        self.prepare()
        self.write_review()

        with (
            patch.dict(os.environ, {adopt.EXECUTOR_SESSION_ENV: "44444444-4444-4444-8444-444444444444"}),
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source") as stop,
            self.assertRaisesRegex(TaskFrontmatterError, "executor Codex session changed"),
        ):
            adopt.execute(self.execute_args())
        stop.assert_not_called()

    def test_live_child_session_drift_writes_nothing_and_does_not_stop(self) -> None:
        child_target = "cc-through-july-2026:1"
        child_pin = adopt.LiveNodePin("source1847_cc_rebuild.md", child_target, "%4", 1004, 2004, "ready", CHILD_SESSION)

        def pane(target: str) -> str:
            return "%4" if target == child_target else self.exact_pane(target)

        def initial_session(pin: adopt.PanePin, protected: tuple[adopt.PanePin, ...], *, may_query: bool) -> str:
            del protected, may_query
            if pin.target == child_target:
                return CHILD_SESSION
            return self.session(pin, (), may_query=False)

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=initial_session),
        ):
            adopt.prepare(self.args(live_node=[child_pin]))
        self.write_review()

        def changed_session(pin: adopt.PanePin, protected: tuple[adopt.PanePin, ...], *, may_query: bool) -> str:
            if pin.target == child_target:
                return "66666666-6666-4666-8666-666666666666"
            return initial_session(pin, protected, may_query=may_query)

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=changed_session),
            patch.object(adopt, "stop_source") as stop,
            self.assertRaisesRegex(TaskFrontmatterError, "live child/descendant Codex session changed"),
        ):
            adopt.execute(self.execute_args())
        stop.assert_not_called()

    def test_live_child_status_drift_writes_nothing_and_does_not_stop(self) -> None:
        child_target = "cc-through-july-2026:1"
        child_pin = adopt.LiveNodePin("source1847_cc_rebuild.md", child_target, "%4", 1004, 2004, "ready", CHILD_SESSION)

        def pane(target: str) -> str:
            return "%4" if target == child_target else self.exact_pane(target)

        def session(pin: adopt.PanePin, protected: tuple[adopt.PanePin, ...], *, may_query: bool) -> str:
            del protected, may_query
            return CHILD_SESSION if pin.target == child_target else self.session(pin, (), may_query=False)

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=session),
        ):
            adopt.prepare(self.args(live_node=[child_pin]))
        self.write_review()

        def changed_status(target: str) -> str:
            return "running" if target == child_target else "ready"

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", side_effect=changed_status),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=session),
            patch.object(adopt, "stop_source") as stop,
            self.assertRaisesRegex(TaskFrontmatterError, "live child/descendant Codex status changed"),
        ):
            adopt.execute(self.execute_args())
        stop.assert_not_called()

    def test_started_close_marker_recovers_after_kill_before_proof(self) -> None:
        packet = self.prepare()
        packet_sha = self.write_review()
        prepared = adopt.prepared_audit(packet, packet_sha, self.review, digest(self.review.read_bytes()), "independent-reviewer")
        prepared_path = self.audit.with_name(f"{self.audit.name}.prepared")
        prepared_path.write_bytes(prepared)
        prepared_path.chmod(0o600)
        proof_path = prepared_path.with_name(f".{prepared_path.name}.owner-stopped")
        source = packet["source_pane"]
        assert isinstance(source, dict)
        omo_codex_stop.write_done_live_close_started(
            proof_path,
            prepared_path,
            str(packet["close_proof_secret"]),
            str(packet["close_proof_commitment"]),
            digest(prepared),
            adopt.SOURCE_TARGET,
            str(source["pane_id"]),
            int(source["pane_pid"]),
            int(source["pane_start_ticks"]),
            adopt.CLOSE_OPERATION,
        )

        def pane(target: str) -> str:
            return "" if target == adopt.SOURCE_TARGET else self.exact_pane(target)

        with (
            patch.object(adopt, "current_pin", side_effect=lambda pin: pin.target != adopt.SOURCE_TARGET),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source") as stop,
            patch.object(omo_codex_stop, "pane_id", return_value=""),
            patch.object(omo_codex_stop, "process_start_ticks", return_value=None),
        ):
            adopt.execute(self.execute_args())
        stop.assert_not_called()
        source_metadata = parse_task_metadata((self.root / adopt.SOURCE_TASK).read_text(), self.root)
        self.assertIsNotNone(source_metadata)
        assert source_metadata is not None
        self.assertEqual("done", source_metadata.status)

    def test_todo_concurrency_after_prepare_writes_nothing_and_does_not_stop(self) -> None:
        self.prepare()
        self.write_review()
        todo = self.root / "TODO.md"
        todo.write_text(todo.read_text() + "concurrent.md other:9\n")
        with patch.object(adopt, "stop_source") as stop, self.assertRaisesRegex(TaskFrontmatterError, "neither the exact initial"):
            adopt.execute(self.execute_args())
        stop.assert_not_called()

    def test_exchange_failure_rolls_back_every_lifecycle_file(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in packet["files"]}
        stopped = {"value": False}
        calls = {"count": 0}
        real_exchange = adopt.rename_exchange

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def fail_third(parent_fd: int, left: str, right: str) -> None:
            calls["count"] += 1
            if calls["count"] == 3:
                raise TaskFrontmatterError("injected exchange failure")
            real_exchange(parent_fd, left, right)

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "rename_exchange", side_effect=fail_third),
            self.assertRaisesRegex(TaskFrontmatterError, "injected exchange failure"),
        ):
            adopt.execute(self.execute_args())
        self.assertEqual(before, {task: (self.root / str(task)).read_bytes() for task in before})

    def test_new_nested_child_during_exchange_rolls_back_lifecycle_files(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in packet["files"]}
        stopped = {"value": False}
        calls = {"count": 0}
        real_exchange = adopt.rename_exchange

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def add_nested_child(parent_fd: int, left: str, right: str) -> None:
            real_exchange(parent_fd, left, right)
            calls["count"] += 1
            if calls["count"] == len(packet["files"]):
                self.write("late_nested.md", task_text(status="running", runat="dw:88", managerat="cc-through-july-2026:1", is_manager=False))

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "rename_exchange", side_effect=add_nested_child),
            self.assertRaisesRegex(TaskFrontmatterError, "active descendant set changed"),
        ):
            adopt.execute(self.execute_args())
        self.assertEqual(before, {task: (self.root / str(task)).read_bytes() for task in before})

    def test_new_direct_source_child_during_exchange_rolls_back_lifecycle_files(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in packet["files"]}
        stopped = {"value": False}
        calls = {"count": 0}
        real_exchange = adopt.rename_exchange

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def add_source_child(parent_fd: int, left: str, right: str) -> None:
            real_exchange(parent_fd, left, right)
            calls["count"] += 1
            if calls["count"] == len(packet["files"]):
                self.write("late_source_child.md", task_text(status="running", runat="dw:89", managerat=adopt.SOURCE_TARGET, is_manager=False))

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "rename_exchange", side_effect=add_source_child),
            self.assertRaisesRegex(TaskFrontmatterError, "retains an active child"),
        ):
            adopt.execute(self.execute_args())
        self.assertEqual(before, {task: (self.root / str(task)).read_bytes() for task in before})

    def test_replaced_parent_before_final_verification_rolls_back_without_commit(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in packet["files"]}
        stopped = {"value": False}
        real_final_check = adopt.final_state_check

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def replace_parent(*args: object, **kwargs: object) -> None:
            original_parent = self.root / "202608"
            original_parent.rename(self.root / "202608-retained")
            original_parent.mkdir()
            for task in (adopt.HISTORICAL_TASK, adopt.ARCHIVE_INDEX):
                entry = next(item for item in packet["files"] if item["task"] == task)
                (original_parent / Path(task).name).write_bytes(adopt.decode(entry["after_base64"], f"{task} after"))
            real_final_check(*args, **kwargs)

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "final_state_check", side_effect=replace_parent),
            self.assertRaisesRegex(CustodyError, "parent identity drifted"),
        ):
            adopt.execute(self.execute_args())

        self.assertFalse(self.audit.exists())
        retained_parent = self.root / "202608-retained"
        for task, data in before.items():
            path = retained_parent / Path(str(task)).name if str(task).startswith("202608/") else self.root / str(task)
            self.assertEqual(data, path.read_bytes())

    def test_new_child_after_final_tree_scan_rolls_back_without_commit(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in packet["files"]}
        stopped = {"value": False}
        real_final_check = adopt.final_state_check

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def add_child_after_scan(*args: object, **kwargs: object) -> None:
            real_final_check(*args, **kwargs)
            self.write("late_after_final_scan.md", task_text(status="running", runat="dw:91", managerat=adopt.SUCCESSOR_TARGET, is_manager=False))

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "final_state_check", side_effect=add_child_after_scan),
            self.assertRaisesRegex(TaskFrontmatterError, "task namespace changed across final"),
        ):
            adopt.execute(self.execute_args())

        self.assertFalse(self.audit.exists())
        self.assertTrue((self.root / "late_after_final_scan.md").exists())
        self.assertEqual(before, {task: (self.root / str(task)).read_bytes() for task in before})

    def test_commit_audit_failure_rolls_back_every_lifecycle_file(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        before = {entry["task"]: (self.root / str(entry["task"])).read_bytes() for entry in packet["files"]}
        stopped = {"value": False}
        real_publish = adopt.publish_or_validate

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def publish(path: Path, data: bytes, label: str) -> None:
            if label == "Source-1923 committed audit":
                raise OSError("injected audit publication failure")
            real_publish(path, data, label)

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "publish_or_validate", side_effect=publish),
            self.assertRaisesRegex(OSError, "injected audit publication failure"),
        ):
            adopt.execute(self.execute_args())
        self.assertEqual(before, {task: (self.root / str(task)).read_bytes() for task in before})

    def test_commit_audit_ambiguous_success_keeps_committed_state(self) -> None:
        self.prepare()
        self.write_review()
        stopped = {"value": False}
        real_publish = adopt.publish_or_validate

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        def publish(path: Path, data: bytes, label: str) -> None:
            real_publish(path, data, label)
            if label == "Source-1923 committed audit":
                raise OSError("injected ambiguous audit publication")

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "publish_or_validate", side_effect=publish),
        ):
            adopt.execute(self.execute_args())

        source = parse_task_metadata((self.root / adopt.SOURCE_TASK).read_text(), self.root)
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual("done", source.status)
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])

    def test_interrupted_recovery_rejects_replaced_parent_without_mutation(self) -> None:
        packet = self.prepare()
        self.write_review()
        historical_entry = next(entry for entry in packet["files"] if entry["task"] == adopt.HISTORICAL_TASK)
        original_parent = self.root / "202608"
        retained_parent = self.root / "202608-retained"
        original_parent.rename(retained_parent)
        original_parent.mkdir()
        historical_path = original_parent / Path(adopt.HISTORICAL_TASK).name
        historical_after = adopt.decode(historical_entry["after_base64"], "historical after")
        historical_path.write_bytes(historical_after)
        (original_parent / Path(adopt.ARCHIVE_INDEX).name).write_bytes((retained_parent / Path(adopt.ARCHIVE_INDEX).name).read_bytes())
        stage_path = original_parent / adopt.stage_name(adopt.HISTORICAL_TASK, str(packet["binding_id"]))
        stage_before = adopt.decode(historical_entry["before_base64"], "historical before")
        stage_path.write_bytes(stage_before)

        with self.assertRaisesRegex(CustodyError, "parent identity drifted"):
            adopt.execute(self.execute_args())

        self.assertEqual(historical_after, historical_path.read_bytes())
        self.assertEqual(stage_before, stage_path.read_bytes())

    def test_committed_recovery_cleans_stages_after_successor_progress(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        stopped = {"value": False}

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "cleanup_committed_stages", side_effect=OSError("injected crash after commit")),
            self.assertRaisesRegex(OSError, "injected crash after commit"),
        ):
            adopt.execute(self.execute_args())

        successor = self.root / adopt.SUCCESSOR_TASK
        progressed = successor.read_bytes() + b"\n(successor made normal post-commit progress.)\n"
        successor.write_bytes(progressed)
        self.write("post_commit_child.md", task_text(status="running", runat="dw:90", managerat=adopt.SUCCESSOR_TARGET, is_manager=False))
        with patch.dict(os.environ, {adopt.EXECUTOR_SESSION_ENV: "77777777-7777-4777-8777-777777777777"}):
            adopt.execute(self.execute_args())

        self.assertEqual(progressed, successor.read_bytes())
        self.assertEqual("committed", json.loads(self.audit.read_text())["state"])
        for entry in packet["files"]:
            task = str(entry["task"])
            stage = (self.root / task).with_name(adopt.stage_name(task, str(packet["binding_id"])))
            self.assertFalse(stage.exists())

    def test_committed_recovery_rejects_replaced_parent_without_deleting_stages(self) -> None:
        self.prepare()
        self.write_review()
        packet = json.loads(self.packet.read_text())
        stopped = {"value": False}

        def pane(target: str) -> str:
            if target == adopt.SOURCE_TARGET:
                return "" if stopped["value"] else "%1"
            return self.exact_pane(target)

        def stop(*_args: object, **_kwargs: object) -> None:
            stopped["value"] = True

        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            patch.object(adopt, "stop_source", side_effect=stop),
            patch.object(adopt, "has_bound_close_proof", side_effect=lambda *_args, **_kwargs: stopped["value"]),
            patch.object(adopt, "cleanup_committed_stages", side_effect=OSError("injected crash after commit")),
            self.assertRaisesRegex(OSError, "injected crash after commit"),
        ):
            adopt.execute(self.execute_args())

        original_parent = self.root / "202608"
        retained_parent = self.root / "202608-retained"
        original_parent.rename(retained_parent)
        original_parent.mkdir()
        historical_entry = next(entry for entry in packet["files"] if entry["task"] == adopt.HISTORICAL_TASK)
        archive_entry = next(entry for entry in packet["files"] if entry["task"] == adopt.ARCHIVE_INDEX)
        (original_parent / Path(adopt.HISTORICAL_TASK).name).write_bytes(adopt.decode(historical_entry["after_base64"], "historical after"))
        (original_parent / Path(adopt.ARCHIVE_INDEX).name).write_bytes(adopt.decode(archive_entry["after_base64"], "archive after"))
        stage_name = adopt.stage_name(adopt.HISTORICAL_TASK, str(packet["binding_id"]))
        fake_stage = original_parent / stage_name
        fake_stage.write_bytes(adopt.decode(historical_entry["before_base64"], "historical before"))
        retained_stage = retained_parent / stage_name
        self.assertTrue(retained_stage.exists())

        with self.assertRaisesRegex(CustodyError, "parent identity drifted"):
            adopt.execute(self.execute_args())

        self.assertTrue(fake_stage.exists())
        self.assertTrue(retained_stage.exists())

    def test_duplicate_successor_owner_rejected_before_packet(self) -> None:
        self.write(
            "duplicate.md",
            task_text(status="blocked", blocked_on="historical conflict", runat=adopt.SUCCESSOR_TARGET, managerat="dw:99", is_manager=True),
        )
        with (
            patch.object(adopt, "current_pin", return_value=True),
            patch.object(adopt, "codex_status", return_value="ready"),
            patch.object(adopt, "exact_pane_id", side_effect=self.exact_pane),
            patch.object(adopt, "live_session_id", side_effect=self.session),
            self.assertRaisesRegex(TaskFrontmatterError, "exactly the live successor and historical"),
        ):
            adopt.prepare(self.args(node=self.task_pins()))
        self.assertFalse(self.packet.exists())


if __name__ == "__main__":
    unittest.main()
