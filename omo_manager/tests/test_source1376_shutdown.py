from __future__ import annotations

import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_source1376_shutdown as shutdown
from omo_manager.omo_agent_status import parse_task_metadata


def task_text(
    runat: str,
    items: tuple[str, ...] = (),
    *,
    status: str = "blocked",
    is_manager: bool = False,
    managerat: str = "mgr:1",
    blocked_on: str = "parked",
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
        *([f"blocked_on: {blocked_on}"] if status == "blocked" else []),
        f"runat: {runat}",
        "tool: codex",
        f"managerat: {managerat}",
        f"is_manager: {str(is_manager).lower()}",
    ]
    if items:
        lines.extend(("pending_task_items:", *(f"  - {item}" for item in items)))
    else:
        lines.append("pending_task_items: []")
    lines.extend(("---", f"body for {runat}"))
    return "\n".join(lines) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def row(row_id: str, task_ref: str, text: str, *, transfer: str = "HOLD-PFX") -> shutdown.PlanRow:
    metadata = parse_task_metadata(text, Path("/tmp"))
    assert metadata is not None
    return shutdown.PlanRow(
        row_id=row_id,
        task_ref=task_ref,
        status=metadata.status,
        is_manager=metadata.is_manager,
        runat=metadata.runat,
        managerat=metadata.managerat,
        n_items=len(metadata.pending_task_items),
        task_sha256=digest(text),
        queue_sha256=shutdown.queue_sha256(metadata.pending_task_items),
        transfer=transfer,
        flags=(),
    )


class Source1376ShutdownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipts = self.root / "receipts"
        self.receipts.mkdir(mode=0o700)
        self.plan = self.root / "plan.txt"
        self.plan.write_text("plan\n", encoding="utf-8")
        self.binding = self.root / "binding.json"
        self.binding.write_text("{}\n", encoding="utf-8")
        self.authority = self.root / "authority.txt"
        self.authority.write_text("authority\n", encoding="utf-8")
        self.packet = self.root.parent / f"{self.root.name}-packet.json"
        self.addCleanup(self.packet.unlink, missing_ok=True)
        self.args = shutdown.Args(self.root, self.plan, self.binding, self.authority, self.receipts, self.packet)
        self.escrow_guard = patch.object(shutdown, "validate_escrow_custody_locked")
        self.escrow_guard.start()
        self.addCleanup(self.escrow_guard.stop)

    def write_todo(self, rows: tuple[tuple[str, str], ...]) -> None:
        values = "\n".join(f"{task_ref} {target}" for task_ref, target in rows)
        (self.root / "TODO.md").write_text(
            f"current:\n{values}\n\nhuman pending:\n\nlow priority:\n\nprevious:\n",
            encoding="utf-8",
        )

    def destination(self) -> str:
        text = task_text(
            "cedit:15",
            (shutdown.AUTHORITY_TEXT,),
            status="long_running",
            is_manager=True,
            managerat="wl:4",
            blocked_on="",
        )
        (self.root / shutdown.DESTINATION_REF).write_text(text, encoding="utf-8")
        return text

    def test_loads_exact_immutable_plan(self) -> None:
        rows = shutdown.load_plan(Path("/ssd1/sichangheagent/amh1376-transfer-plan-20260902.md"))
        self.assertEqual(84, len(rows))
        self.assertEqual("202608/amh1232_term_eval.md", rows["17"].task_ref)

    def test_loads_exact_supplemental_execution_binding(self) -> None:
        rows = shutdown.load_execution_rows(shutdown.PLAN_PATH, shutdown.EXECUTION_BINDING_PATH)
        self.assertEqual(84, len(rows))
        self.assertEqual("done", rows["29"].status)
        self.assertEqual(shutdown.ROW29_CURRENT_SHA256, rows["29"].task_sha256)
        self.assertEqual(
            "ebd60e85d87ae6c7fca6280ee8988c990becbb8027c7b22b5ac5f64a5f02619b",
            shutdown.DESTINATION_INITIAL_SHA256,
        )

    def test_transfers_historical_nonblocked_queue_and_recovers_partial_write(self) -> None:
        source_text = task_text(
            "agent_managers:1",
            ("first", "second"),
            status="long_running",
            is_manager=True,
            managerat="amh:1",
            blocked_on="",
        )
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        destination_text = self.destination()
        self.write_todo((("source.md", "agent_managers:1"), (shutdown.DESTINATION_REF, "cedit:15")))
        rows = {"01": row("01", "source.md", source_text)}
        real_replace = shutdown.replace_if_unchanged_locked
        calls = 0

        def interrupt_second(path: Path, text: str, before: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            real_replace(path, text, before)  # type: ignore[arg-type]

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "replace_if_unchanged_locked", side_effect=interrupt_second),
            self.assertRaises(KeyboardInterrupt),
        ):
            shutdown.transfer_rows(self.args, rows, ("01",), digest(destination_text))

        self.assertTrue((self.root / shutdown.TRANSFER_JOURNAL).is_file())
        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
        ):
            receipt = shutdown.transfer_rows(self.args, rows, ("01",), digest(destination_text))

        self.assertFalse((self.root / shutdown.TRANSFER_JOURNAL).exists())
        source_metadata = parse_task_metadata(source.read_text(encoding="utf-8"), self.root)
        destination_metadata = parse_task_metadata((self.root / shutdown.DESTINATION_REF).read_text(encoding="utf-8"), self.root)
        assert source_metadata is not None
        assert destination_metadata is not None
        self.assertEqual("blocked", source_metadata.status)
        self.assertEqual((), source_metadata.pending_task_items)
        self.assertEqual((shutdown.AUTHORITY_TEXT, "first", "second"), destination_metadata.pending_task_items)
        self.assertEqual(["01"], receipt["rows"])

    def test_group_transfer_preserves_duplicate_multiplicity(self) -> None:
        first_text = task_text("agent_managers:64", ("same item",), status="long_running", blocked_on="")
        second_text = task_text("agent_managers:65", ("same item",), status="long_running", blocked_on="")
        (self.root / "first.md").write_text(first_text, encoding="utf-8")
        (self.root / "second.md").write_text(second_text, encoding="utf-8")
        destination_text = self.destination()
        self.write_todo(
            (
                ("first.md", "agent_managers:64"),
                ("second.md", "agent_managers:65"),
                (shutdown.DESTINATION_REF, "cedit:15"),
            )
        )
        rows = {
            "44": row("44", "first.md", first_text),
            "45": row("45", "second.md", second_text),
        }
        with (
            patch.object(shutdown, "DESTINATION_INITIAL_SHA256", digest(destination_text)),
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
        ):
            shutdown.transfer_rows(self.args, rows, ("44", "45"), digest(destination_text))

        metadata = parse_task_metadata((self.root / shutdown.DESTINATION_REF).read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual((shutdown.AUTHORITY_TEXT, "same item", "same item"), metadata.pending_task_items)

    def test_shared_external_source_transfers_without_mutating_survivor(self) -> None:
        source_text = task_text("agent_managers:5", ("move me",))
        survivor_text = task_text("agent_managers:5", (), managerat="hwl:3")
        (self.root / "source.md").write_text(source_text, encoding="utf-8")
        survivor = self.root / "survivor.md"
        survivor.write_text(survivor_text, encoding="utf-8")
        destination_text = self.destination()
        self.write_todo(
            (
                ("source.md", "agent_managers:5"),
                ("survivor.md", "agent_managers:5"),
                (shutdown.DESTINATION_REF, "cedit:15"),
            )
        )
        rows = {
            "28": row("28", "source.md", source_text),
            "29": row("29", "survivor.md", survivor_text),
        }
        with (
            patch.object(shutdown, "DESTINATION_INITIAL_SHA256", digest(destination_text)),
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
        ):
            shutdown.transfer_rows(self.args, rows, ("28",), digest(destination_text))

        self.assertEqual(survivor_text, survivor.read_text(encoding="utf-8"))

    def test_completed_external_survivor_remains_protected_while_source_closes(self) -> None:
        source_text = task_text("agent_managers:5", ("move me",))
        survivor_text = task_text("agent_managers:5", (), status="done", managerat="hwl:3", blocked_on="")
        source = self.root / "source.md"
        survivor = self.root / "survivor.md"
        source.write_text(source_text, encoding="utf-8")
        survivor.write_text(survivor_text, encoding="utf-8")
        destination_text = self.destination()
        self.write_todo(
            (
                ("source.md", "agent_managers:5"),
                ("survivor.md", "agent_managers:5"),
                (shutdown.DESTINATION_REF, "cedit:15"),
            )
        )
        rows = {
            "28": row("28", "source.md", source_text),
            "29": row("29", "survivor.md", survivor_text),
        }
        with (
            patch.object(shutdown, "DESTINATION_INITIAL_SHA256", digest(destination_text)),
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
        ):
            shutdown.transfer_rows(self.args, rows, ("28",), digest(destination_text))

        with (
            patch.object(shutdown, "DESTINATION_INITIAL_SHA256", digest(destination_text)),
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=""),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
        ):
            receipt = shutdown.close_row(self.args, rows, "28")

        self.assertEqual("protected-shared-missing-target", receipt["mode"])
        self.assertEqual(survivor_text, survivor.read_text(encoding="utf-8"))
        survivor_metadata = parse_task_metadata(survivor.read_text(encoding="utf-8"), self.root)
        assert survivor_metadata is not None
        self.assertEqual("done", survivor_metadata.status)

    def test_missing_target_close_recovers_after_todo_write(self) -> None:
        source_text = task_text("agent_managers:9")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo((("source.md", "agent_managers:9"),))
        rows = {"01": row("01", "source.md", source_text, transfer="-")}
        real_replace = shutdown.replace_if_unchanged_locked
        calls = 0

        def interrupt_task(path: Path, text: str, before: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            real_replace(path, text, before)  # type: ignore[arg-type]

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=""),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
            patch.object(shutdown, "replace_if_unchanged_locked", side_effect=interrupt_task),
            self.assertRaises(KeyboardInterrupt),
        ):
            shutdown.close_row(self.args, rows, "01")

        journal = shutdown.close_journal_path(self.receipts, ("01",))
        self.assertTrue(journal.is_file())
        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=""),
        ):
            shutdown.close_row(self.args, rows, "01")

        metadata = parse_task_metadata(source.read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("done", metadata.status)
        self.assertEqual(("previous",), shutdown.task_row_sections(self.root, source.resolve()))
        self.assertFalse(journal.exists())

    def test_missing_target_close_preserves_absent_todo_custody(self) -> None:
        source_text = task_text("agent_managers:9")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo(())
        todo_before = (self.root / "TODO.md").read_bytes()
        rows = {"01": row("01", "source.md", source_text, transfer="-")}

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=""),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
        ):
            receipt = shutdown.close_row(self.args, rows, "01")

        metadata = parse_task_metadata(source.read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("done", metadata.status)
        self.assertEqual([], receipt["todo_before_sections"])
        self.assertEqual([], receipt["todo_after_sections"])
        self.assertEqual(todo_before, (self.root / "TODO.md").read_bytes())

    def test_missing_shared_target_close_preserves_mixed_todo_custody(self) -> None:
        first_text = task_text("amh:11")
        second_text = task_text("amh:11", managerat="wl:2")
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text(first_text, encoding="utf-8")
        second.write_text(second_text, encoding="utf-8")
        self.write_todo((("first.md", "amh:11"),))
        rows = {
            "71": row("71", "first.md", first_text, transfer="-"),
            "72": row("72", "second.md", second_text, transfer="-"),
        }

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=""),
            patch.object(
                shutdown,
                "authoritative_active_target_task_paths",
                return_value=(first.resolve(), second.resolve()),
            ),
        ):
            receipt = shutdown.close_internal_shared_pair(self.args, rows)

        self.assertEqual("coordinated-shared-missing-target", receipt["mode"])
        self.assertEqual({"71": ["current"], "72": []}, receipt["todo_before_sections"])
        self.assertEqual({"71": ["previous"], "72": []}, receipt["todo_after_sections"])
        self.assertEqual(("previous",), shutdown.task_row_sections(self.root, first.resolve()))
        self.assertEqual((), shutdown.task_row_sections(self.root, second.resolve()))
        for path in (first, second):
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), self.root)
            assert metadata is not None
            self.assertEqual("done", metadata.status)

    def test_live_close_records_intent_then_uses_bound_stop(self) -> None:
        source_text = task_text("cfg:1")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo((("source.md", "cfg:1"),))
        rows = {"01": row("01", "source.md", source_text, transfer="-")}
        live = True

        def pane(target: str) -> str:
            return "%42" if live and target in {"cfg:1", "%42"} else ""

        def stopped(*_args: object, **_kwargs: object) -> str:
            nonlocal live
            live = False
            return "11111111-2222-3333-4444-555555555555"

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", side_effect=pane),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
            patch.object(shutdown, "stop_bound_target", side_effect=stopped) as stop_target,
        ):
            receipt = shutdown.close_row(self.args, rows, "01")

        stop_target.assert_called_once()
        self.assertEqual("live", receipt["mode"])
        metadata = parse_task_metadata(source.read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("done", metadata.status)

    def test_coordinated_shared_close_stops_one_pane_after_both_intents(self) -> None:
        first_text = task_text("amh:11")
        second_text = task_text("amh:11", managerat="wl:2")
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text(first_text, encoding="utf-8")
        second.write_text(second_text, encoding="utf-8")
        self.write_todo((("first.md", "amh:11"), ("second.md", "amh:11")))
        rows = {
            "71": row("71", "first.md", first_text, transfer="-"),
            "72": row("72", "second.md", second_text, transfer="-"),
        }
        live = True

        def pane(target: str) -> str:
            return "%42" if live and target in {"amh:11", "%42"} else ""

        def stopped(*_args: object, **_kwargs: object) -> str:
            nonlocal live
            for path in (first, second):
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"), self.root)
                assert metadata is not None
                self.assertEqual(shutdown.DONE_CLOSE_IN_PROGRESS, metadata.blocked_on)
            live = False
            return "11111111-2222-3333-4444-555555555555"

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", side_effect=pane),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(first.resolve(), second.resolve())),
            patch.object(shutdown, "stop_bound_target", side_effect=stopped) as stop_target,
        ):
            receipt = shutdown.close_internal_shared_pair(self.args, rows)

        stop_target.assert_called_once()
        self.assertEqual(["71", "72"], receipt["rows"])
        for path in (first, second):
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), self.root)
            assert metadata is not None
            self.assertEqual("done", metadata.status)

    def test_authority_drift_prevents_task_write_and_pane_stop(self) -> None:
        source_text = task_text("cfg:1")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo((("source.md", "cfg:1"),))
        rows = {"01": row("01", "source.md", source_text, transfer="-")}

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", side_effect=shutdown.TaskFrontmatterError("authority drift")),
            patch.object(shutdown, "stop_bound_target") as stop_target,
            self.assertRaisesRegex(shutdown.TaskFrontmatterError, "authority drift"),
        ):
            shutdown.close_row(self.args, rows, "01")

        self.assertEqual(source_text, source.read_text(encoding="utf-8"))
        self.assertFalse(shutdown.close_journal_path(self.receipts, ("01",)).exists())
        stop_target.assert_not_called()

    def test_escrow_destination_drift_prevents_task_write_and_pane_stop(self) -> None:
        self.escrow_guard.stop()
        source_text = task_text("cfg:1")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        destination_text = self.destination()
        destination = self.root / shutdown.DESTINATION_REF
        destination.write_text(destination_text + "unreviewed escrow edit\n", encoding="utf-8")
        self.write_todo((("source.md", "cfg:1"), (shutdown.DESTINATION_REF, "cedit:15")))
        rows = {"01": row("01", "source.md", source_text, transfer="-")}

        with (
            patch.object(shutdown, "DESTINATION_INITIAL_SHA256", digest(destination_text)),
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value="%42"),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
            patch.object(shutdown, "stop_bound_target") as stop_target,
            self.assertRaisesRegex(shutdown.TaskFrontmatterError, "escrow owner drifted"),
        ):
            shutdown.close_row(self.args, rows, "01")

        self.assertEqual(source_text, source.read_text(encoding="utf-8"))
        self.assertFalse(shutdown.close_journal_path(self.receipts, ("01",)).exists())
        stop_target.assert_not_called()

    def test_row17_rejects_a_different_pane_before_durable_intent(self) -> None:
        source_text = task_text("agent_managers:39")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo((("source.md", "agent_managers:39"),))
        rows = {shutdown.COMPLETED_SHELL_ROW: row(shutdown.COMPLETED_SHELL_ROW, "source.md", source_text, transfer="-")}

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value="%1856"),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
            patch.object(shutdown, "stop_bound_target") as stop_target,
            self.assertRaisesRegex(shutdown.TaskFrontmatterError, "reviewed exact pane"),
        ):
            shutdown.close_row(self.args, rows, shutdown.COMPLETED_SHELL_ROW)

        self.assertEqual(source_text, source.read_text(encoding="utf-8"))
        self.assertFalse(shutdown.close_journal_path(self.receipts, (shutdown.COMPLETED_SHELL_ROW,)).exists())
        stop_target.assert_not_called()

    def test_row17_absent_after_intent_does_not_terminalize(self) -> None:
        source_text = task_text("agent_managers:39")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo((("source.md", "agent_managers:39"),))
        rows = {shutdown.COMPLETED_SHELL_ROW: row(shutdown.COMPLETED_SHELL_ROW, "source.md", source_text, transfer="-")}

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=shutdown.COMPLETED_SHELL_PANE),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
            patch.object(shutdown, "resume_pane_close", return_value={}),
        ):
            shutdown.close_row(self.args, rows, shutdown.COMPLETED_SHELL_ROW)

        journal = shutdown.close_journal_path(self.receipts, (shutdown.COMPLETED_SHELL_ROW,))
        self.assertTrue(journal.is_file())
        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value=""),
            patch.object(shutdown, "close_exited_codex_shell_with_task_receipt") as close_shell,
            self.assertRaisesRegex(shutdown.TaskFrontmatterError, "reviewed exited shell is absent"),
        ):
            shutdown.close_row(self.args, rows, shutdown.COMPLETED_SHELL_ROW)

        metadata = parse_task_metadata(source.read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("blocked", metadata.status)
        self.assertEqual(shutdown.DONE_CLOSE_IN_PROGRESS, metadata.blocked_on)
        self.assertFalse(shutdown.close_receipt_path(self.receipts, (shutdown.COMPLETED_SHELL_ROW,)).exists())
        self.assertTrue(journal.exists())
        close_shell.assert_not_called()

    def test_packet_builder_holds_complete_custody_locks(self) -> None:
        rows = {"01": row("01", "source.md", task_text("cfg:1"), transfer="-")}
        custody_paths = (self.root / "one.md", self.root / "two.md")
        active_files: set[Path] = set()
        active_targets: set[str] = set()
        membership = False

        @contextmanager
        def membership_lock(_root: Path):
            nonlocal membership
            membership = True
            try:
                yield
            finally:
                membership = False

        @contextmanager
        def target_lock(_root: Path, target: str):
            active_targets.add(target)
            try:
                yield
            finally:
                active_targets.remove(target)

        @contextmanager
        def file_lock(path: Path):
            active_files.add(path)
            try:
                yield
            finally:
                active_files.remove(path)

        def assert_complete_snapshot(*_args: object) -> None:
            self.assertTrue(membership)
            self.assertEqual({"cedit:15", "cfg:1"}, active_targets)
            self.assertTrue({*custody_paths, self.packet}.issubset(active_files))

        with (
            patch.object(shutdown, "root_membership_lock", side_effect=membership_lock),
            patch.object(shutdown, "task_target_lock", side_effect=target_lock),
            patch.object(shutdown, "task_file_lock", side_effect=file_lock),
            patch.object(shutdown, "closure_custody_paths", return_value=custody_paths),
            patch.object(shutdown, "validate_operation_bindings_locked", side_effect=assert_complete_snapshot),
            patch.object(shutdown, "validate_escrow_custody_locked", side_effect=assert_complete_snapshot),
            patch.object(shutdown, "build_packet_locked", side_effect=assert_complete_snapshot) as locked_builder,
        ):
            shutdown.build_packet(self.args, rows, "a" * 64)

        locked_builder.assert_called_once_with(self.args, rows, "a" * 64)
        self.assertFalse(membership)
        self.assertEqual(set(), active_targets)
        self.assertEqual(set(), active_files)

    def test_tampered_pane_journal_prevents_stop(self) -> None:
        source_text = task_text("cfg:1")
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.write_todo((("source.md", "cfg:1"),))
        rows = {"01": row("01", "source.md", source_text, transfer="-")}

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value="%42"),
            patch.object(shutdown, "authoritative_active_target_task_paths", return_value=(source.resolve(),)),
            patch.object(shutdown, "resume_pane_close", return_value={}),
        ):
            shutdown.close_row(self.args, rows, "01")

        journal_path = shutdown.close_journal_path(self.receipts, ("01",))
        loaded = shutdown.read_private_json(journal_path, required=True)
        assert loaded is not None
        tampered = dict(loaded[0])
        originals = dict(tampered["tasks_original"])  # type: ignore[arg-type]
        originals["source.md"] = originals["source.md"] + "tampered\n"
        tampered["tasks_original"] = originals
        shutdown.replace_private_json(journal_path, loaded[1], tampered, final=False)

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            patch.object(shutdown, "exact_pane_id", return_value="%42"),
            patch.object(shutdown, "stop_bound_target") as stop_target,
            self.assertRaisesRegex(shutdown.TaskFrontmatterError, "does not rederive"),
        ):
            shutdown.close_row(self.args, rows, "01")
        stop_target.assert_not_called()

    def test_prepopulated_foreign_transfer_receipt_does_not_bypass_preflight(self) -> None:
        source_text = task_text("agent_managers:1", ("item",))
        source = self.root / "source.md"
        source.write_text(source_text, encoding="utf-8")
        self.destination()
        self.write_todo((("source.md", "agent_managers:1"), (shutdown.DESTINATION_REF, "cedit:15")))
        rows = {"01": row("01", "source.md", source_text)}
        shutdown.write_private_json(
            self.receipts / "transfer-01.json",
            {
                "schema": "omo-source1376-transfer/v1",
                "rows": ["01"],
                "destination_before_sha256": shutdown.DESTINATION_INITIAL_SHA256,
                "destination_after_sha256": "a" * 64,
            },
            final=True,
        )

        with (
            patch.object(shutdown, "load_execution_rows", return_value=rows),
            patch.object(shutdown, "validate_authority", return_value=f"{shutdown.AUTHORITY_REF}:3-3"),
            self.assertRaisesRegex(shutdown.TaskFrontmatterError, "not bound"),
        ):
            shutdown.validate_initial_state(self.args, rows)

        self.assertEqual(source_text, source.read_text(encoding="utf-8"))

    def test_transfer_receipts_follow_digest_chain_not_filename_order(self) -> None:
        second_before = "b" * 64
        second_after = "c" * 64
        shutdown.write_private_json(
            self.receipts / "transfer-99.json",
            {
                "schema": "omo-source1376-transfer/v1",
                "destination_before_sha256": shutdown.DESTINATION_INITIAL_SHA256,
                "destination_after_sha256": second_before,
            },
            final=True,
        )
        shutdown.write_private_json(
            self.receipts / "transfer-01.json",
            {
                "schema": "omo-source1376-transfer/v1",
                "destination_before_sha256": second_before,
                "destination_after_sha256": second_after,
            },
            final=True,
        )

        receipts = shutdown.transfer_receipts(self.receipts)

        self.assertEqual(shutdown.DESTINATION_INITIAL_SHA256, receipts[0]["destination_before_sha256"])
        self.assertEqual(second_after, receipts[1]["destination_after_sha256"])


if __name__ == "__main__":
    unittest.main()
