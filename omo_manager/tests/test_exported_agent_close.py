from __future__ import annotations

import hashlib
import base64
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_exported_agent_close import IndeterminateClose, execute, prepare
from omo_manager.omo_exported_agent_close import replace_held
from omo_manager.omo_exported_agent_close import rename_exchange
from omo_manager.omo_repository_custody import CustodyError, absolute_file_binding, hold_absolute


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def task_text(status: str, target: str, manager: str, is_manager: bool, queue: tuple[str, ...]) -> str:
    pending = "pending_task_items: []" if not queue else "pending_task_items:\n" + "".join(f"  - {item}\n" for item in queue).rstrip()
    blocker = "persistent manager role" if status == "long_running" else "retired work"
    return (
        "---\nversion: v1.0.0\n"
        f"status: {status}\nblocked_on: {blocker}\nrunat: {target}\ntool: codex\nmanagerat: {manager}\n"
        f"is_manager: {str(is_manager).lower()}\n{pending}\n---\nbody\n"
    )


def export_text(ref: str, text: str, root: Path) -> str:
    metadata = parse_task_metadata(text, root)
    assert metadata is not None
    queue = list(metadata.pending_task_items)
    queue_sha = digest(json.dumps(queue, ensure_ascii=False, separators=(",", ":")))
    items = "- ordered pending items: none\n" if not queue else "- ordered pending items:\n" + "".join(
        f"  {index}. {json.dumps(item)}\n" for index, item in enumerate(queue, 1)
    )
    return (
        f"# Source 1398\n\n## {ref}\n\n- target: {json.dumps(metadata.runat)}\n"
        f"- manager: {json.dumps(metadata.managerat)}\n- status: {json.dumps(metadata.status)}\n"
        f"- manager role: {json.dumps(str(metadata.is_manager).lower())}\n"
        f"- blocker: {json.dumps(metadata.blocked_on)}\n- task-file SHA-256: `{digest(text)}`\n"
        f"- ordered pending-item count: {len(queue)}\n- ordered pending-items SHA-256: `{queue_sha}`\n{items}"
    )


class ExportedAgentCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "manager_mail").mkdir()
        (self.root / "manager_mail").chmod(0o700)
        self.authority = self.root / "manager_mail" / "source1398.txt"
        authority_text = "get all their pending task items, write them down into another file, not necessarily a task file. Then you simply close that agent\n"
        self.authority.write_text(authority_text)
        self.authority.chmod(0o600)
        self.envelope = self.root / "source1398_request.md"
        self.envelope.write_text(
            '<human_instruction authoritative="true" source="manager_mail/source1398.txt:1-1">\n'
            f"{authority_text}</human_instruction>\n"
        )
        self.export = self.root / "SOURCE1398_AGENT_CLOSURE_TODO.md"
        (self.root / "private").mkdir(mode=0o700)
        self.packet = self.root / "private" / "packet.json"
        self.audit = self.root / "private" / "audit.json"

    def write_case(
        self,
        mode: str,
        *,
        target: str = "gone:1",
        queue: tuple[str, ...] = ("keep this",),
    ) -> Namespace:
        is_manager = mode == "absent-manager-previous"
        status = "long_running" if is_manager else "blocked"
        task = self.root / "task.md"
        text = task_text(status, target, "mgr:1", is_manager, queue)
        task.write_text(text)
        self.export.write_text(export_text("task.md", text, self.root))
        section = "previous" if is_manager else "current"
        row = "" if mode == "absent-worker-unindexed" else f"task.md {target}\n"
        todo = "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\n"
        if row:
            todo = todo.replace(f"{section}:\n", f"{section}:\n{row}")
        (self.root / "TODO.md").write_text(todo)
        return Namespace(
            root=self.root,
            task=Path("task.md"),
            mode=mode,
            target=target,
            task_sha256=digest(text),
            todo_sha256=digest(todo),
            export=self.export,
            export_sha256=digest(self.export.read_text()),
            authority=self.authority,
            authority_sha256=digest(self.authority.read_text()),
            authority_lines=(1, 1),
            authority_envelope=Path("source1398_request.md"),
            authority_envelope_sha256=digest(self.envelope.read_text()),
            destination_target="hcfg:1",
            protected_task=None,
            protected_sha256="",
            pane_id="",
            audit=self.audit,
            packet=self.packet,
        )

    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_prepare_absent_manager_previous(self, _pane: object) -> None:
        args = self.write_case("absent-manager-previous")
        prepare(args)
        packet = json.loads(self.packet.read_text())
        self.assertEqual(packet["mode"], "absent-manager-previous")
        self.assertNotEqual(packet["task_before_sha256"], packet["task_after_sha256"])

    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_prepare_absent_worker_without_todo_row(self, _pane: object) -> None:
        prepare(self.write_case("absent-worker-unindexed"))
        self.assertTrue(self.packet.is_file())

    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="%9")
    def test_prepare_shared_live_preserves_pane(self, _pane: object, owners: object) -> None:
        args = self.write_case("shared-live-worker", target="shared:1")
        protected = self.root / "sibling.md"
        protected.write_text(task_text("blocked", "shared:1", "mgr:1", False, ()))
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(protected.read_text())
        args.pane_id = "%9"
        owners.return_value = (self.root / "task.md", protected)
        prepare(args)
        self.assertEqual(json.loads(self.packet.read_text())["pane_id"], "%9")

    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_prepare_shared_absent_manager_preserves_sibling(self, _pane: object, owners: object) -> None:
        args = self.write_case("shared-absent-manager", target="shared:1", queue=())
        task = self.root / "task.md"
        text = task_text("blocked", "shared:1", "mgr:1", True, ())
        task.write_text(text)
        self.export.write_text(export_text("task.md", text, self.root))
        args.task_sha256 = digest(text)
        args.export_sha256 = digest(self.export.read_text())
        sibling = self.root / "sibling.md"
        sibling.write_text(task_text("blocked", "shared:1", "mgr:2", False, ("preserve",)))
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(sibling.read_text())
        owners.return_value = (task, sibling)
        prepare(args)
        packet = json.loads(self.packet.read_text())
        self.assertEqual(packet["mode"], "shared-absent-manager")
        self.assertEqual(packet["pane_id"], "")
        self.assertEqual(packet["protected"]["sha256"], args.protected_sha256)

    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="%9")
    def test_rejects_shared_absent_manager_when_target_becomes_live(self, _pane: object, owners: object) -> None:
        args = self.write_case("shared-absent-manager", target="shared:1", queue=())
        text = task_text("blocked", "shared:1", "mgr:1", True, ())
        (self.root / "task.md").write_text(text)
        self.export.write_text(export_text("task.md", text, self.root))
        args.task_sha256 = digest(text)
        args.export_sha256 = digest(self.export.read_text())
        sibling = self.root / "sibling.md"
        sibling.write_text(task_text("blocked", "shared:1", "mgr:2", False, ()))
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(sibling.read_text())
        owners.return_value = (self.root / "task.md", sibling)
        with self.assertRaisesRegex(TaskFrontmatterError, "shared-absent manager"):
            prepare(args)

    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_rejects_source_as_its_own_protected_sibling(self, _pane: object) -> None:
        args = self.write_case("shared-absent-manager", target="shared:1", queue=())
        text = task_text("blocked", "shared:1", "mgr:1", True, ())
        task = self.root / "task.md"
        task.write_text(text)
        self.export.write_text(export_text("task.md", text, self.root))
        args.task_sha256 = digest(text)
        args.export_sha256 = digest(self.export.read_text())
        args.protected_task = Path("task.md")
        args.protected_sha256 = digest(text)
        with self.assertRaisesRegex(TaskFrontmatterError, "must differ"):
            prepare(args)

    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_rejects_changed_export_queue(self, _pane: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        changed = self.export.read_text().replace("keep this", "lost this")
        self.export.write_text(changed)
        args.export_sha256 = digest(changed)
        with self.assertRaisesRegex(TaskFrontmatterError, "complete ordered queue"):
            prepare(args)

    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_rejects_wrong_todo_section(self, _pane: object) -> None:
        args = self.write_case("absent-manager-previous")
        todo = (self.root / "TODO.md").read_text().replace(
            "current:\n", "current:\ntask.md gone:1\n"
        ).replace("previous:\ntask.md gone:1", "previous:")
        (self.root / "TODO.md").write_text(todo)
        args.todo_sha256 = digest(todo)
        with self.assertRaisesRegex(TaskFrontmatterError, "wrong section"):
            prepare(args)

    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="%9")
    def test_rejects_extra_shared_owner(self, _pane: object, owners: object) -> None:
        args = self.write_case("shared-live-worker", target="shared:1")
        sibling = self.root / "sibling.md"
        sibling.write_text(task_text("blocked", "shared:1", "mgr:1", False, ()))
        extra = self.root / "extra.md"
        extra.write_text(task_text("blocked", "shared:1", "mgr:1", False, ()))
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(sibling.read_text())
        args.pane_id = "%9"
        owners.return_value = (self.root / "task.md", sibling, extra)
        with self.assertRaisesRegex(TaskFrontmatterError, "exactly the bound two owners"):
            prepare(args)

    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_rejects_authority_envelope_swap_after_semantic_check(self, _pane: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        original = self.envelope.read_text()

        def swap(*_args: object) -> str:
            self.envelope.write_text(original + "replacement\n")
            return "source1398_request.md"

        with patch("omo_manager.omo_exported_agent_close.read_park_authority_envelope", side_effect=swap):
            with self.assertRaisesRegex(TaskFrontmatterError, "changed between semantic"):
                prepare(args)

    def test_descriptor_exchange_rejects_final_window_substitution(self) -> None:
        path = self.root / "leaf.txt"
        path.write_text("before")
        _, identity, ancestors = absolute_file_binding(path.resolve(), "test leaf")
        held = hold_absolute(identity, ancestors)

        def substitute(_held: object) -> None:
            replacement = self.root / "replacement.txt"
            replacement.write_text("before")
            replacement.replace(path)

        try:
            with patch("omo_manager.omo_exported_agent_close.validate_held_absolute", side_effect=substitute):
                with self.assertRaisesRegex(TaskFrontmatterError, "substitution"):
                    replace_held(held, "after")
            self.assertEqual(path.read_text(), "before")
        finally:
            os.close(held.descriptor)
            for descriptor in reversed(held.directories):
                os.close(descriptor)

    def test_descriptor_exchange_preserves_displaced_leaf_on_post_exchange_substitution(self) -> None:
        path = self.root / "leaf.txt"
        path.write_text("before")
        _, identity, ancestors = absolute_file_binding(path.resolve(), "test leaf")
        held = hold_absolute(identity, ancestors)
        calls = 0

        def exchange_then_substitute(parent_fd: int, left: str, right: str) -> None:
            nonlocal calls
            rename_exchange(parent_fd, left, right)
            calls += 1
            if calls == 1:
                intruder = self.root / "intruder.txt"
                intruder.write_text("intruder")
                intruder.replace(path)

        try:
            with patch("omo_manager.omo_exported_agent_close.rename_exchange", side_effect=exchange_then_substitute):
                with self.assertRaisesRegex(TaskFrontmatterError, "indeterminate"):
                    replace_held(held, "after")
            self.assertEqual(path.read_text(), "intruder")
            preserved = [candidate for candidate in self.root.glob(".leaf.txt.*") if candidate.read_text() == "before"]
            self.assertEqual(len(preserved), 1)
        finally:
            os.close(held.descriptor)
            for descriptor in reversed(held.directories):
                os.close(descriptor)

    def test_descriptor_exchange_restores_on_final_parent_fsync_failure(self) -> None:
        path = self.root / "leaf.txt"
        path.write_text("before")
        _, identity, ancestors = absolute_file_binding(path.resolve(), "test leaf")
        held = hold_absolute(identity, ancestors)
        real_fsync = os.fsync
        calls = 0

        def fail_second(descriptor: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected parent fsync failure")
            real_fsync(descriptor)

        try:
            with patch("omo_manager.omo_exported_agent_close.os.fsync", side_effect=fail_second):
                with self.assertRaisesRegex(TaskFrontmatterError, "restored"):
                    replace_held(held, "after")
            self.assertEqual(path.read_text(), "before")
        finally:
            os.close(held.descriptor)
            for descriptor in reversed(held.directories):
                os.close(descriptor)

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_execute_rejects_task_drift_before_write(self, _pane: object, _report: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        prepare(args)
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        (self.root / "task.md").write_text((self.root / "task.md").read_text() + "drift\n")
        with self.assertRaisesRegex(CustodyError, "identity drifted"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertFalse(self.audit.exists())

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_execute_commits_metadata_without_tmux_stop(self, pane: object, _report: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        prepare(args)
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        metadata = parse_task_metadata((self.root / "task.md").read_text(), self.root)
        self.assertEqual(metadata.status, "done")
        self.assertEqual(metadata.pending_task_items, ())
        self.assertTrue(self.audit.is_file())
        self.assertEqual(pane.call_count, 3)

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_execute_recovers_after_todo_write(self, _pane: object, _report: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        prepare(args)
        packet = json.loads(self.packet.read_text())
        audit_keys = (
            "schema", "mode", "task", "target", "pane_id", "task_before_sha256",
            "todo_before_sha256", "task_after_sha256", "todo_after_sha256", "export",
            "export_sha256", "authority", "authority_sha256", "protected", "binding_id",
        )
        prepared = {key: packet[key] for key in audit_keys}
        prepared["state"] = "prepared"
        Path(f"{self.audit}.prepared").write_text(json.dumps(prepared, sort_keys=True, separators=(",", ":")) + "\n")
        Path(f"{self.audit}.prepared").chmod(0o600)
        (self.root / "TODO.md").write_text(base64.b64decode(packet["todo_after_base64"]).decode())
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertEqual(parse_task_metadata((self.root / "task.md").read_text(), self.root).status, "done")
        self.assertTrue(self.audit.is_file())

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_execute_recovers_after_partial_rollback_replaced_task_inode(self, _pane: object, _report: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        prepare(args)
        packet = json.loads(self.packet.read_text())
        audit_keys = (
            "schema", "mode", "task", "target", "pane_id", "task_before_sha256",
            "todo_before_sha256", "task_after_sha256", "todo_after_sha256", "export",
            "export_sha256", "authority", "authority_sha256", "protected", "binding_id",
        )
        prepared = {key: packet[key] for key in audit_keys}
        prepared["state"] = "prepared"
        Path(f"{self.audit}.prepared").write_text(json.dumps(prepared, sort_keys=True, separators=(",", ":")) + "\n")
        Path(f"{self.audit}.prepared").chmod(0o600)
        task = self.root / "task.md"
        replacement = self.root / "task-replacement.md"
        replacement.write_bytes(task.read_bytes())
        replacement.replace(task)
        (self.root / "TODO.md").write_text(base64.b64decode(packet["todo_after_base64"]).decode())
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertEqual(parse_task_metadata(task.read_text(), self.root).status, "done")
        self.assertTrue(self.audit.is_file())

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", side_effect=CustodyError("not authenticated"))
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_execute_rejects_plain_review_json(self, _pane: object, _report: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        prepare(args)
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_text(json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}))
        with self.assertRaisesRegex(CustodyError, "not authenticated"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="%9")
    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    def test_execute_shared_live_keeps_pane_and_sibling(self, owners: object, pane: object, _report: object) -> None:
        args = self.write_case("shared-live-worker", target="shared:1")
        sibling = self.root / "sibling.md"
        sibling.write_text(task_text("blocked", "shared:1", "mgr:1", False, ()))
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(sibling.read_text())
        args.pane_id = "%9"
        owners.side_effect = [
            (self.root / "task.md", sibling),
            (self.root / "task.md", sibling),
            (sibling,),
        ]
        prepare(args)
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertEqual(parse_task_metadata((self.root / "task.md").read_text(), self.root).status, "done")
        self.assertEqual(parse_task_metadata(sibling.read_text(), self.root).status, "blocked")
        self.assertEqual(pane.call_count, 3)

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    def test_execute_shared_absent_manager_keeps_sibling(self, owners: object, pane: object, _report: object) -> None:
        args = self.write_case("shared-absent-manager", target="shared:1", queue=())
        text = task_text("blocked", "shared:1", "mgr:1", True, ())
        task = self.root / "task.md"
        task.write_text(text)
        self.export.write_text(export_text("task.md", text, self.root))
        args.task_sha256 = digest(text)
        args.export_sha256 = digest(self.export.read_text())
        sibling = self.root / "sibling.md"
        sibling.write_text(task_text("blocked", "shared:1", "mgr:2", False, ("preserve",)))
        sibling_before = sibling.read_bytes()
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(sibling.read_text())
        owners.side_effect = [(task, sibling), (task, sibling), (sibling,)]
        prepare(args)
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertEqual(parse_task_metadata(task.read_text(), self.root).status, "done")
        self.assertEqual(sibling.read_bytes(), sibling_before)
        self.assertEqual(pane.call_count, 3)

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    @patch("omo_manager.omo_exported_agent_close.authoritative_active_target_task_paths")
    def test_execute_shared_absent_manager_rejects_postwrite_owner_drift(self, owners: object, _pane: object, _report: object) -> None:
        args = self.write_case("shared-absent-manager", target="shared:1", queue=())
        text = task_text("blocked", "shared:1", "mgr:1", True, ())
        task = self.root / "task.md"
        task.write_text(text)
        self.export.write_text(export_text("task.md", text, self.root))
        args.task_sha256 = digest(text)
        args.export_sha256 = digest(self.export.read_text())
        sibling = self.root / "sibling.md"
        sibling.write_text(task_text("blocked", "shared:1", "mgr:2", False, ()))
        extra = self.root / "extra.md"
        extra.write_text(task_text("blocked", "shared:1", "mgr:2", False, ()))
        args.protected_task = Path("sibling.md")
        args.protected_sha256 = digest(sibling.read_text())
        owners.side_effect = [(task, sibling), (task, sibling), (sibling, extra)]
        prepare(args)
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        task_before = task.read_bytes()
        todo_before = (self.root / "TODO.md").read_bytes()
        with self.assertRaisesRegex(TaskFrontmatterError, "bytes were restored"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertEqual(task.read_bytes(), task_before)
        self.assertEqual((self.root / "TODO.md").read_bytes(), todo_before)
        self.assertFalse(self.audit.exists())

    @patch("omo_manager.omo_exported_agent_close.authenticated_report", return_value={"producer_target": "review:1"})
    @patch("omo_manager.omo_exported_agent_close.park_target_pane_id", return_value="")
    def test_indeterminate_task_write_keeps_todo_forward_for_recovery(self, _pane: object, _report: object) -> None:
        args = self.write_case("absent-worker-unindexed")
        prepare(args)
        packet = json.loads(self.packet.read_text())
        packet_sha = digest(self.packet.read_text())
        review = self.root / "private" / "review.json"
        review.write_bytes(b"message:\n" + json.dumps({"schema": "omo-exported-agent-close-review/v1", "verdict": "PASS", "packet_sha256": packet_sha}).encode())
        from omo_manager import omo_exported_agent_close as module

        real_replace = module.replace_held
        calls = 0

        def replace_then_fail(held: object, text: str) -> object:
            nonlocal calls
            calls += 1
            result = real_replace(held, text)
            if calls == 2:
                raise IndeterminateClose("injected indeterminate task publication")
            return result

        with patch("omo_manager.omo_exported_agent_close.replace_held", side_effect=replace_then_fail):
            with self.assertRaisesRegex(IndeterminateClose, "injected"):
                execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_text())))
        self.assertEqual(digest((self.root / "task.md").read_text()), packet["task_after_sha256"])
        self.assertEqual(digest((self.root / "TODO.md").read_text()), packet["todo_after_sha256"])
        self.assertTrue(Path(f"{self.audit}.prepared").is_file())
        self.assertFalse(self.audit.exists())


if __name__ == "__main__":
    unittest.main()
