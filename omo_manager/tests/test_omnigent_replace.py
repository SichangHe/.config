"""Real fence-store and filesystem transactions with explicit simulated lifecycle.

These tests do not certify a production native-writer barrier or host restart.
"""

from __future__ import annotations

import tempfile
import threading
import types
import unittest
import importlib.machinery
import io
import json
import os
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from typing import NoReturn, TypedDict, final, override
from unittest.mock import patch

from omo_manager import omo_omnigent_replace as replacement
from omo_manager.omo_manager_replace import ReplaceError, Snapshot, replace_snapshot
from omo_manager.omo_omnigent_fence import FenceStore, Operation, Rejected, digest
from omo_manager.omo_task_lock import task_file_lock
from omo_manager.omo_omnigent_replace import (
    ExactPacket,
    FileImage,
    ManifestItem,
    Preparation,
    ProcessPin,
    ReviewApproval,
    RuntimeState,
    execute,
    load_packet,
    prepare,
    raw_queue,
    reconcile,
)


class SimulatedCrash(BaseException):
    pass


def checked[T](value: object, expected: type[T]) -> T:
    assert isinstance(value, expected), str(value)
    return value


class BuildArguments(TypedDict):
    authority: Path
    task: Path
    todo: Path
    workspace: Path
    manifest: Path
    output: Path


@final
class SimulatedLifecycle:
    """A test server ledger, deliberately not a production process verifier."""

    def __init__(self, runtime: RuntimeState):
        self.runtime = runtime
        self.receipts: dict[str, dict[str, object]] = {}
        self.calls: list[str] = []
        self.crash: str = ""
        self.available = True
        self.hide_delivery = False

    def ready(self, packet: ExactPacket) -> Rejected | None:
        return None if self.available else Rejected("native_barrier_unavailable", packet.operation_id)

    def observe(self) -> RuntimeState:
        return self.runtime

    def _record(self, phase: str, receipt: dict[str, object]) -> dict[str, object]:
        self.calls.append(phase)
        self.receipts[phase] = receipt
        if self.crash == phase:
            raise SimulatedCrash(phase)
        return receipt

    def quiesce(self, packet: ExactPacket) -> dict[str, object]:
        expected = packet.expected
        return self._record(
            "old_quiesced",
            {
                "operation_id": packet.operation_id,
                "evidence_sha256": digest(b"test native barrier"),
                **{key: getattr(expected, key) for key in ("session_id", "runner_id", "thread_id", "process_sha256", "host_sha256")},
                "native_writers_gone": True,
                "pending_launches_absent": True,
                "transport_drained": True,
            },
        )

    def prepare_successor(self, packet: ExactPacket) -> dict[str, object]:
        return self._record(
            "new_prepared",
            {
                "operation_id": packet.operation_id,
                "evidence_sha256": digest(b"test staged creation"),
                "new_session_id": packet.successor_id,
                "workspace": packet.workspace.path,
                "unrestricted": True,
                "message_count": 0,
                "runner_absent": True,
            },
        )

    def deliver(self, packet: ExactPacket) -> dict[str, object]:
        return self._record(
            "delivery_confirmed",
            {
                "operation_id": packet.operation_id,
                "evidence_sha256": digest(b"test delivery"),
                "new_session_id": packet.successor_id,
                "delivery_id": packet.delivery_id,
                "prompt_sha256": digest(packet.prompt.encode()),
            },
        )

    def inspect_receipt(self, packet: ExactPacket, phase: str) -> dict[str, object] | Rejected:
        if phase == "delivery_confirmed" and self.hide_delivery:
            return Rejected("delivery_unknown", "test connection lost")
        return self.receipts.get(phase, Rejected("effect_unknown", packet.operation_id))

    def reconcile_effect(self, packet: ExactPacket, phase: str) -> Rejected:
        return Rejected("no_test_repair", f"{packet.operation_id}: {phase}")


@final
class Fixture:
    def __init__(self, test: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        test.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root_patch = patch.object(replacement, "DEFAULT_ROOT", self.root)
        self.root_patch.start()
        test.addCleanup(self.root_patch.stop)
        self.authority = self.root / "source.txt"
        self.authority.write_bytes(b"1\nquoted Source1957 choice\n")
        self.authority_patch = patch.object(replacement, "AUTHORITY_SHA256", digest(self.authority.read_bytes()))
        self.authority_patch.start()
        test.addCleanup(self.authority_patch.stop)
        self.task = self.root / replacement.TASK_NAME
        self.todo = self.root / "TODO.md"
        self.workspace = self.root / replacement.WORKSPACE_NAME
        self.workspace.mkdir()
        self.local_config = self.root / "manager.env"
        self.local_config.write_text(f"OMO_SOURCE1957_WORKSPACE={self.workspace}\n")
        self.config_patch = patch.object(replacement, "configuration_source", return_value=self.local_config)
        self.config_patch.start()
        test.addCleanup(self.config_patch.stop)
        self.workspace_patch = patch.dict(replacement.LOCAL_ENV, {"OMO_SOURCE1957_WORKSPACE": str(self.workspace)})
        self.workspace_patch.start()
        test.addCleanup(self.workspace_patch.stop)
        self.real_package_digest = replacement.installed_package_digest
        self.package_patch = patch.object(replacement, "installed_package_digest", return_value=replacement.OMNIGENT_SOURCE_SHA256)
        self.package_patch.start()
        test.addCleanup(self.package_patch.stop)
        self.queue = tuple(f"exact original queue item {index}" for index in range(9))
        queue = "".join(f"  - '{item}'\r\n" for item in self.queue)
        self.task_before = (
            "---\r\nversion: v1.0.0\r\nstatus: blocked\r\nblocked_on: review.md\r\n"
            f"runat: omnigent://{replacement.OLD_SESSION_ID}\r\ntool: codex\r\nmanagerat: dw:59\r\nis_manager: false\r\n"
            f"pending_task_items:\r\n{queue}---\r\nOld assembled prompt must never be sent.\r\n\r\nOld undelivered input.\r\n"
        ).encode()
        self.todo_before = (
            f"current:\r\nother.md dw:2\r\n\thuman.md h:1\r\nhuman pending:\r\n{replacement.TASK_NAME}\tomnigent://{replacement.OLD_SESSION_ID}  \r\n"
            "low priority:\r\nlater.md dw:4\r\nprevious:\r\nold.md dw:8\r\n"
        ).encode()
        self.task.write_bytes(self.task_before)
        self.todo.write_bytes(self.todo_before)
        self.store = FenceStore(self.root / "fence.sqlite")
        self.runtime = RuntimeState(
            replacement.OLD_SESSION_ID,
            "test-runner",
            replacement.OLD_HOST_ID,
            replacement.OLD_THREAD_ID,
            replacement.encoded(b'{"session":"old"}'),
            replacement.encoded(b'["three finished turns"]'),
            replacement.encoded(b'{"host":"single-runner"}'),
            replacement.encoded(b'{"route":"old"}'),
            (ProcessPin(345, 987, "test-boot", digest(b"test argv"), "native-domain"),),
            ("test-runner",),
            (),
            0,
            0,
            str(self.workspace),
        )
        self.backend = SimulatedLifecycle(self.runtime)
        self.inputs = Preparation(
            self.authority,
            self.task,
            self.todo,
            self.workspace,
            replacement.implementation_sources(),
            replacement.OMNIGENT_SOURCE_SHA256,
            tuple(ManifestItem(index, digest(item.encode()), "constraint" if index in (6, 7) else "open", f"reviewed work {index}", f"queue:{index}") for index, item in enumerate(self.queue)),
        )


class ReplacementTests(unittest.TestCase):
    _fixture: Fixture | None = None

    @property
    def fixture(self) -> Fixture:
        assert self._fixture is not None
        return self._fixture

    @override
    def setUp(self) -> None:
        self._fixture = Fixture(self)

    def prepared(self) -> tuple[ExactPacket, ReviewApproval]:
        inputs = replacement.bind_preparation(self.fixture.inputs)
        self.assertIsInstance(inputs, Preparation, str(inputs))
        assert isinstance(inputs, Preparation)
        result = prepare(inputs, self.fixture.store, self.fixture.backend)
        self.assertIsInstance(result, ExactPacket, str(result))
        assert isinstance(result, ExactPacket)
        review = self.fixture.root / "review.txt"
        review.write_text(f"approve Source1957 replacement {result.sha256}\n")
        return result, ReviewApproval(result.sha256, FileImage.capture(review).pin)

    def test_complete_transfer_preserves_every_unapproved_byte_and_nine_items(self) -> None:
        packet, approval = self.prepared()
        self.assertEqual("prepared", checked(self.fixture.store.get(packet.operation_id), Operation).phase)
        self.assertEqual([], self.fixture.backend.calls)
        self.assertEqual(packet, load_packet(packet.serialize()))
        result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertIsInstance(result, Operation, str(result))
        assert isinstance(result, Operation)
        self.assertEqual("delivery_confirmed", result.phase)
        self.assertEqual(
            self.fixture.task_before.replace(b"status: blocked\r\nblocked_on: review.md\r\n", b"status: running\r\n").replace(replacement.OLD_SESSION_ID.encode(), packet.successor_id.encode()),
            self.fixture.task.read_bytes(),
        )
        self.assertEqual(self.fixture.todo_before.replace(replacement.OLD_SESSION_ID.encode(), packet.successor_id.encode()), self.fixture.todo.read_bytes())
        self.assertEqual(raw_queue(self.fixture.task_before), raw_queue(self.fixture.task.read_bytes()))
        self.assertNotIn("Old assembled", packet.prompt)
        self.assertNotIn("Old undelivered", packet.prompt)
        self.assertEqual(["old_quiesced", "new_prepared", "delivery_confirmed"], self.fixture.backend.calls)
        self.assertTrue(self.fixture.store.is_fenced(replacement.OLD_SESSION_ID))
        self.assertEqual(result, execute(packet, approval, self.fixture.store, self.fixture.backend))
        self.assertEqual(3, len(self.fixture.backend.calls))

    def test_unavailable_native_barrier_rejects_before_fence(self) -> None:
        packet, approval = self.prepared()
        self.fixture.backend.available = False
        self.assertEqual("native_barrier_unavailable", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertFalse(self.fixture.store.is_fenced(replacement.OLD_SESSION_ID))
        self.assertEqual(self.fixture.task_before, self.fixture.task.read_bytes())

    def test_authority_and_packet_review_are_exact(self) -> None:
        packet, approval = self.prepared()
        wrong = replace(approval, packet_sha256=digest(b"different"))
        self.assertEqual("review_required", checked(execute(packet, wrong, self.fixture.store, self.fixture.backend), Rejected).code)
        self.fixture.authority.write_bytes(b"different authority")
        self.assertEqual("packet_changed_or_invalid", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertEqual([], self.fixture.backend.calls)

    def test_changed_task_queue_todo_and_runtime_reject_before_mutation(self) -> None:
        for drift in ("task", "queue", "todo", "runner", "process", "items", "host"):
            with self.subTest(drift=drift):
                packet, approval = self.prepared()
                if drift in {"task", "queue"}:
                    self.fixture.task.write_bytes(self.fixture.task_before + b"drift" if drift == "task" else self.fixture.task_before.replace(b"item 0", b"item changed"))
                elif drift == "todo":
                    self.fixture.todo.write_bytes(self.fixture.todo_before + b"other.md dw:99\r\n")
                elif drift == "runner":
                    self.fixture.backend.runtime = replace(self.fixture.runtime, runner_id="stale-runner")
                elif drift == "process":
                    self.fixture.backend.runtime = replace(self.fixture.runtime, processes=(replace(self.fixture.runtime.processes[0], start_ticks=988),))
                elif drift == "items":
                    self.fixture.backend.runtime = replace(self.fixture.runtime, items_b64=replacement.encoded(b"new message"))
                else:
                    self.fixture.backend.runtime = replace(self.fixture.runtime, online_runner_ids=("test-runner", "another-runner"))
                result = execute(packet, approval, self.fixture.store, self.fixture.backend)
                self.assertIsInstance(result, Rejected)
                assert isinstance(result, Rejected)
                self.assertFalse(self.fixture.store.is_fenced(replacement.OLD_SESSION_ID))
                cancelled = self.fixture.store.cancel_prepared(packet.operation_id, replacement._evidence(packet, new_session_id=packet.successor_id, successor_absent=True))
                self.assertIsInstance(cancelled, Operation)
                assert isinstance(cancelled, Operation)
                self.fixture.task.write_bytes(self.fixture.task_before)
                self.fixture.todo.write_bytes(self.fixture.todo_before)
                self.fixture.backend.runtime = self.fixture.runtime

    def test_message_between_observation_and_fence_invalidates_generation(self) -> None:
        packet, approval = self.prepared()

        def racing_observation() -> RuntimeState:
            admission = self.fixture.store.admit([replacement.OLD_SESSION_ID], "http")
            assert not isinstance(admission, Rejected)
            self.fixture.store.finish(admission)
            return self.fixture.runtime

        with patch.object(self.fixture.backend, "observe", side_effect=racing_observation):
            self.assertIsInstance(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected)
        self.assertEqual([], self.fixture.backend.calls)

    def test_each_lifecycle_crash_requires_inspection_and_never_repeats_effect(self) -> None:
        for phase in ("old_quiesced", "new_prepared", "delivery_confirmed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.fixture.store = FenceStore(Path(directory) / "fence.sqlite")
                self.fixture.task.write_bytes(self.fixture.task_before)
                self.fixture.todo.write_bytes(self.fixture.todo_before)
                self.fixture.backend = SimulatedLifecycle(self.fixture.runtime)
                packet, approval = self.prepared()
                self.fixture.backend.crash = phase
                with self.assertRaises(SimulatedCrash):
                    execute(packet, approval, self.fixture.store, self.fixture.backend)
                self.fixture.store = FenceStore(Path(directory) / "fence.sqlite")
                self.assertEqual("reconciliation_required", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
                recovered = reconcile(packet, approval, self.fixture.store, self.fixture.backend)
                self.assertIsInstance(recovered, Operation, str(recovered))
                assert isinstance(recovered, Operation)
                self.fixture.backend.crash = ""
                completed = execute(packet, approval, self.fixture.store, self.fixture.backend)
                self.assertIsInstance(completed, Operation, str(completed))
                assert isinstance(completed, Operation)
                self.assertEqual(1, self.fixture.backend.calls.count(phase))

    def test_unknown_delivery_never_resends_or_allocates_another_successor(self) -> None:
        packet, approval = self.prepared()
        self.fixture.backend.crash = "delivery_confirmed"
        with self.assertRaises(SimulatedCrash):
            execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.fixture.backend.hide_delivery = True
        self.assertEqual("delivery_unknown", checked(reconcile(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertEqual("reconciliation_required", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertEqual(1, self.fixture.backend.calls.count("new_prepared"))
        self.assertEqual(1, self.fixture.backend.calls.count("delivery_confirmed"))
        self.assertEqual(packet.successor_id, checked(self.fixture.store.get(packet.operation_id), Operation).spec.new_session_id)

    def test_second_file_cas_failure_rolls_back_first_without_touching_queue(self) -> None:
        packet, approval = self.prepared()

        def fail_todo(expected: Snapshot, data: bytes, label: str) -> Snapshot:
            if label == "todo":
                raise ReplaceError("injected TODO conflict")
            return replace_snapshot(expected, data, label)

        with patch.object(replacement, "replace_snapshot", side_effect=fail_todo):
            result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("publication_uncertain", checked(result, Rejected).code)
        self.assertEqual(self.fixture.task_before, self.fixture.task.read_bytes())
        self.assertEqual(self.fixture.todo_before, self.fixture.todo.read_bytes())
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)
        self.assertTrue(self.fixture.store.is_fenced(replacement.OLD_SESSION_ID))

    def test_crash_between_file_exchange_and_receipt_is_not_guessed(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.store.record_evidence

        def crash_done(operation_id: str, key: str, receipt: dict[str, object]) -> Operation | Rejected:
            if key == "file_task_done":
                raise SimulatedCrash("after file exchange")
            return original(operation_id, key, receipt)

        with patch.object(self.fixture.store, "record_evidence", side_effect=crash_done), self.assertRaises(SimulatedCrash):
            execute(packet, approval, self.fixture.store, self.fixture.backend)
        result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("publication_uncertain", checked(result, Rejected).code)
        self.assertEqual("file_reconciliation_required", checked(reconcile(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_crash_after_durable_task_receipt_resumes_only_remaining_file(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.store.record_evidence

        def crash_done(operation_id: str, key: str, receipt: dict[str, object]) -> Operation | Rejected:
            result = original(operation_id, key, receipt)
            if key == "file_task_done":
                raise SimulatedCrash("after durable task receipt")
            return result

        with patch.object(self.fixture.store, "record_evidence", side_effect=crash_done), self.assertRaises(SimulatedCrash):
            execute(packet, approval, self.fixture.store, self.fixture.backend)
        task_pin = FileImage.capture(self.fixture.task).pin
        result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertIsInstance(result, Operation, str(result))
        assert isinstance(result, Operation)
        self.assertEqual(task_pin, FileImage.capture(self.fixture.task).pin)
        self.assertEqual(1, self.fixture.backend.calls.count("delivery_confirmed"))

    def test_bad_manifest_or_changed_sources_rejected(self) -> None:
        incomplete = replace(self.fixture.inputs, manifest=self.fixture.inputs.manifest[:-1])
        self.assertIsInstance(prepare(incomplete, self.fixture.store, self.fixture.backend), Rejected)
        packet, approval = self.prepared()
        altered = replace(packet, sources=tuple(replace(pin, sha256=digest(b"different source")) for pin in packet.sources))
        self.assertIsInstance(execute(altered, approval, self.fixture.store, self.fixture.backend), Rejected)
        self.assertEqual([], self.fixture.backend.calls)

    def test_completed_replay_does_not_require_available_backend(self) -> None:
        packet, approval = self.prepared()
        complete = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.fixture.backend.available = False
        self.assertEqual(complete, execute(packet, approval, self.fixture.store, self.fixture.backend))

    def test_claim_crash_requires_read_only_delivery_inspection(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.store.claim_delivery

        def crash_claim(operation_id: str, delivery_id: str, prompt_sha256: str) -> NoReturn:
            original(operation_id, delivery_id, prompt_sha256)
            raise SimulatedCrash("claimed before dispatch")

        with patch.object(self.fixture.store, "claim_delivery", side_effect=crash_claim), self.assertRaises(SimulatedCrash):
            execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("effect_unknown", checked(reconcile(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertEqual("reconciliation_required", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_saved_lifecycle_result_never_repeats_effect_after_advance_crash(self) -> None:
        for phase in ("old_quiesced", "new_prepared", "delivery_confirmed"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.fixture.store = FenceStore(Path(directory) / "fence.sqlite")
                self.fixture.task.write_bytes(self.fixture.task_before)
                self.fixture.todo.write_bytes(self.fixture.todo_before)
                self.fixture.backend = SimulatedLifecycle(self.fixture.runtime)
                packet, approval = self.prepared()
                original = self.fixture.store.advance

                def crash_advance(operation_id: str, next_phase: str, receipt: dict[str, object]) -> Operation | Rejected:
                    if next_phase == phase:
                        raise SimulatedCrash("result already reconciled")
                    return original(operation_id, next_phase, receipt)

                with patch.object(self.fixture.store, "advance", side_effect=crash_advance), self.assertRaises(SimulatedCrash):
                    execute(packet, approval, self.fixture.store, self.fixture.backend)
                completed = execute(packet, approval, self.fixture.store, self.fixture.backend)
                self.assertIsInstance(completed, Operation, str(completed))
                assert isinstance(completed, Operation)
                self.assertEqual(1, self.fixture.backend.calls.count(phase))

    def test_todo_exchange_without_receipt_remains_uncertain(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.store.record_evidence

        def crash_todo(operation_id: str, key: str, receipt: dict[str, object]) -> Operation | Rejected:
            if key == "file_todo_done":
                raise SimulatedCrash("TODO exchanged, receipt absent")
            return original(operation_id, key, receipt)

        with patch.object(self.fixture.store, "record_evidence", side_effect=crash_todo), self.assertRaises(SimulatedCrash):
            execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("publication_uncertain", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertEqual("file_reconciliation_required", checked(reconcile(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_foreign_rebind_after_task_receipt_prevents_todo_exchange(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.store.record_evidence

        def rebind_task(operation_id: str, key: str, receipt: dict[str, object]) -> Operation | Rejected:
            result = original(operation_id, key, receipt)
            if key == "file_task_done":
                self.fixture.task.write_bytes(b"foreign task state")
            return result

        with patch.object(self.fixture.store, "record_evidence", side_effect=rebind_task):
            result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("publication_uncertain", checked(result, Rejected).code)
        self.assertEqual(b"foreign task state", self.fixture.task.read_bytes())
        self.assertEqual(self.fixture.todo_before, self.fixture.todo.read_bytes())
        self.assertNotIn("file_todo_intent", checked(self.fixture.store.get(packet.operation_id), Operation).receipts)
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_foreign_rebind_during_todo_exchange_rolls_todo_back(self) -> None:
        packet, approval = self.prepared()

        def rebind_during_todo(expected: Snapshot, data: bytes, label: str) -> Snapshot:
            if label == "todo":
                self.fixture.task.write_bytes(b"foreign task state")
            return replace_snapshot(expected, data, label)

        with patch.object(replacement, "replace_snapshot", side_effect=rebind_during_todo):
            result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("publication_uncertain", checked(result, Rejected).code)
        self.assertEqual(b"foreign task state", self.fixture.task.read_bytes())
        self.assertEqual(self.fixture.todo_before, self.fixture.todo.read_bytes())
        operation = checked(self.fixture.store.get(packet.operation_id), Operation)
        self.assertEqual(("new_prepared", "uncertain"), (operation.phase, operation.status))
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_foreign_todo_rebind_does_not_prevent_independent_task_rollback(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.store.record_evidence

        def rebind_todo(operation_id: str, key: str, receipt: dict[str, object]) -> Operation | Rejected:
            result = original(operation_id, key, receipt)
            if key == "file_todo_done":
                self.fixture.todo.write_bytes(b"foreign TODO state")
            return result

        with patch.object(self.fixture.store, "record_evidence", side_effect=rebind_todo):
            result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("publication_uncertain", checked(result, Rejected).code)
        self.assertEqual(b"foreign TODO state", self.fixture.todo.read_bytes())
        self.assertEqual(self.fixture.task_before, self.fixture.task.read_bytes())
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_post_dispatch_drift_is_uncertain_and_does_not_claim_delivery_rollback(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.backend.deliver

        def drift_after_dispatch(value: ExactPacket) -> dict[str, object]:
            receipt = original(value)
            self.fixture.task.write_bytes(b"foreign post-dispatch task")
            return receipt

        with patch.object(self.fixture.backend, "deliver", side_effect=drift_after_dispatch):
            result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("committed_files_changed", checked(result, Rejected).code)
        operation = checked(self.fixture.store.get(packet.operation_id), Operation)
        self.assertEqual(("committed", "uncertain"), (operation.phase, operation.status))
        self.assertEqual(1, self.fixture.backend.calls.count("delivery_confirmed"))
        self.assertEqual(b"foreign post-dispatch task", self.fixture.task.read_bytes())
        self.assertEqual("reconciliation_required", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)

    def test_delivery_reconciliation_rejects_competing_task_owner(self) -> None:
        packet, approval = self.prepared()
        original = self.fixture.backend.deliver

        def competing_owner_after_dispatch(value: ExactPacket) -> dict[str, object]:
            receipt = original(value)
            (self.fixture.root / "competing.md").write_bytes(self.fixture.task.read_bytes())
            return receipt

        with patch.object(self.fixture.backend, "deliver", side_effect=competing_owner_after_dispatch):
            result = execute(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertEqual("task_ownership_conflict", checked(result, Rejected).code)
        self.assertEqual("task_ownership_conflict", checked(reconcile(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertEqual("uncertain", checked(self.fixture.store.get(packet.operation_id), Operation).status)
        self.assertEqual(1, self.fixture.backend.calls.count("delivery_confirmed"))

    def test_supported_writer_cannot_enter_during_dispatch_acceptance(self) -> None:
        packet, approval = self.prepared()
        blocked: list[bool] = []
        original = self.fixture.backend.deliver

        def writer() -> None:
            try:
                with task_file_lock(self.fixture.task, timeout_s=0.05):
                    blocked.append(False)
            except TimeoutError:
                blocked.append(True)

        def dispatch_under_lock(value: ExactPacket) -> dict[str, object]:
            thread = threading.Thread(target=writer)
            thread.start()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            return original(value)

        with patch.object(self.fixture.backend, "deliver", side_effect=dispatch_under_lock):
            self.assertIsInstance(execute(packet, approval, self.fixture.store, self.fixture.backend), Operation)
        self.assertEqual([True], blocked)

    def test_lookalike_root_and_source_names_do_not_satisfy_authority(self) -> None:
        packet, approval = self.prepared()
        clone = self.fixture.root / "clone"
        clone.mkdir()
        clone_task, clone_todo = clone / self.fixture.task.name, clone / self.fixture.todo.name
        clone_task.write_bytes(self.fixture.task_before)
        clone_todo.write_bytes(self.fixture.todo_before)
        copied = replace(packet, task=FileImage.capture(clone_task), todo=FileImage.capture(clone_todo))
        self.assertEqual("packet_changed_or_invalid", checked(replacement.validate(copied), Rejected).code)
        fake_workspace = clone / replacement.WORKSPACE_NAME
        fake_workspace.mkdir()
        copied_workspace = replace(packet, workspace=replacement.WorkspacePin.capture(fake_workspace))
        self.assertEqual("packet_changed_or_invalid", checked(replacement.validate(copied_workspace), Rejected).code)
        fake_sources = []
        for name in replacement.SOURCE_NAMES:
            source = clone / name
            source.write_bytes(b"unreviewed same-name source")
            fake_sources.append(FileImage.capture(source).pin)
        changed = replace(packet, sources=tuple(fake_sources))
        self.assertEqual("packet_changed_or_invalid", checked(replacement.validate(changed), Rejected).code)
        self.assertEqual("review_required", checked(execute(packet, replace(approval, evidence=packet.authority.pin), self.fixture.store, self.fixture.backend), Rejected).code)

    def test_competing_owner_and_pending_marker_reject_before_fence(self) -> None:
        self.fixture.task.write_bytes(self.fixture.task_before + b"\r\n(pending)\r\nnew unrecorded goal\r\n")
        self.assertIsInstance(prepare(self.fixture.inputs, self.fixture.store, self.fixture.backend), Rejected)
        self.fixture.task.write_bytes(self.fixture.task_before)
        packet, approval = self.prepared()
        other = self.fixture.root / "competing.md"
        other.write_bytes(self.fixture.task_before)
        self.assertEqual("task_ownership_conflict", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        self.assertFalse(self.fixture.store.is_fenced(replacement.OLD_SESSION_ID))

    def test_control_prepare_recovers_reserved_packet_after_output_crash(self) -> None:
        preparation_path = self.fixture.root / "preparation.json"
        packet_path, approval_path = self.fixture.root / "packet.json", self.fixture.root / "approval.txt"
        bound = replacement.bind_preparation(self.fixture.inputs)
        self.assertIsInstance(bound, Preparation, str(bound))
        assert isinstance(bound, Preparation)
        payload = bound.serialize()
        preparation_path.write_bytes(payload)
        preparation_path.chmod(0o600)

        def call_control(action: str = "prepare", packet_sha256: str = "", approval_sha256: str = "") -> dict[str, object] | Rejected:
            return replacement.dispatch_control(
                action, preparation_path, packet_path, approval_path, digest(payload) if action == "prepare" else "", packet_sha256, approval_sha256, self.fixture.store, self.fixture.backend
            )

        with patch.object(replacement, "create_snapshot", side_effect=SimulatedCrash("before output")), self.assertRaises(SimulatedCrash):
            call_control()
        reserved = self.fixture.store.for_old_session(replacement.OLD_SESSION_ID)
        self.assertIsNotNone(reserved)
        assert reserved is not None
        result = call_control()
        self.assertIsInstance(result, dict, str(result))
        assert isinstance(result, dict)
        self.assertEqual(reserved.spec.operation_id, result["operation_id"])
        self.assertEqual(result, call_control())
        packet_digest = result["packet_sha256"]
        assert isinstance(packet_digest, str)
        inspected = call_control("inspect", packet_digest)
        assert isinstance(inspected, dict)
        self.assertEqual("prepared", inspected["phase"])
        approval_path.write_text(f"approve Source1957 replacement {packet_digest}\n")
        approval_path.chmod(0o600)
        self.assertEqual("control_digest_mismatch", checked(call_control("execute", packet_digest, digest(b"wrong review")), Rejected).code)
        self.assertEqual([], self.fixture.backend.calls)
        completed = call_control("execute", packet_digest, digest(approval_path.read_bytes()))
        assert isinstance(completed, dict)
        self.assertEqual("delivery_confirmed", completed["phase"])

    def test_only_proven_owned_successor_initialization_can_be_repaired(self) -> None:
        packet, approval = self.prepared()
        self.fixture.backend.crash = "new_prepared"
        with self.assertRaises(SimulatedCrash):
            execute(packet, approval, self.fixture.store, self.fixture.backend)
        receipt = self.fixture.backend.receipts["new_prepared"]
        with (
            patch.object(self.fixture.backend, "inspect_receipt", return_value=Rejected("owned_successor_incomplete", "exact reserved initialization")),
            patch.object(self.fixture.backend, "reconcile_effect", return_value=receipt) as repair,
        ):
            result = reconcile(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertIsInstance(result, Operation, str(result))
        assert isinstance(result, Operation)
        self.assertEqual("new_prepared", result.phase)
        repair.assert_called_once_with(packet, "new_prepared")
        self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)

    def test_narrow_repair_is_never_used_for_other_unknown_effects(self) -> None:
        for phase, code in (("old_quiesced", "owned_successor_incomplete"), ("delivery_confirmed", "owned_successor_incomplete"), ("new_prepared", "effect_unknown")):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.fixture.store = FenceStore(Path(directory) / "fence.sqlite")
                self.fixture.task.write_bytes(self.fixture.task_before)
                self.fixture.todo.write_bytes(self.fixture.todo_before)
                self.fixture.backend = SimulatedLifecycle(self.fixture.runtime)
                packet, approval = self.prepared()
                self.fixture.backend.crash = phase
                with self.assertRaises(SimulatedCrash):
                    execute(packet, approval, self.fixture.store, self.fixture.backend)
                with patch.object(self.fixture.backend, "inspect_receipt", return_value=Rejected(code, "test uncertainty")), patch.object(self.fixture.backend, "reconcile_effect") as repair:
                    self.assertEqual(code, checked(reconcile(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
                repair.assert_not_called()

    def test_unrelated_later_import_does_not_invalidate_reviewed_packet(self) -> None:
        packet, approval = self.prepared()
        unrelated = types.ModuleType("omo_manager.unrelated_later_tool")
        unrelated.__file__ = str(Path(replacement.__file__).parent / "unrelated_later_tool.py")
        with patch.dict("sys.modules", {unrelated.__name__: unrelated}):
            self.assertIsInstance(execute(packet, approval, self.fixture.store, self.fixture.backend), Operation)

    def test_build_preparation_cli_authors_current_schema_without_reserving_owner(self) -> None:
        from omo_manager import omo_omnigent_server as server

        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            package_root = private / "installed" / "omnigent"
            package_root.mkdir(parents=True)
            (package_root / "__init__.py").write_text('"""Fixture installed OmniGent sources."""\n')
            package_sha256 = server.source_digest(package_root)
            package_spec = importlib.machinery.ModuleSpec("omnigent", loader=None, is_package=True)
            package_spec.submodule_search_locations = [str(package_root)]
            manifest_path, constraints_path, output = private / "manifest.json", private / "constraints.json", private / "preparation.json"
            manifest_path.write_text(replacement.canonical([{key: value for key, value in asdict(item).items() if key != "queue_item_sha256"} for item in self.fixture.inputs.manifest]))
            constraints_path.write_text(replacement.canonical([{"source": str(self.fixture.authority), "quote": "quoted Source1957 choice"}]))
            bound_file = private / "backend.json"
            bound_file.write_text("reviewed backend configuration fixture\n")
            command = [
                "build-preparation",
                "--authority",
                str(self.fixture.authority),
                "--task",
                str(self.fixture.task),
                "--todo",
                str(self.fixture.todo),
                "--workspace",
                str(self.fixture.workspace),
                "--manifest",
                str(manifest_path),
                "--constraints",
                str(constraints_path),
                "--bound-file",
                str(bound_file),
                "--output",
                str(output),
            ]
            with (
                patch.object(server.importlib.metadata, "version", return_value=server.OMNIGENT_VERSION),
                patch.object(server.importlib.util, "find_spec", return_value=package_spec),
                patch.object(server, "OMNIGENT_SOURCE_SHA256", package_sha256),
                patch.object(replacement, "OMNIGENT_SOURCE_SHA256", package_sha256),
                patch.object(replacement, "installed_package_digest", side_effect=self.fixture.real_package_digest),
                redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(0, replacement.main(command))
                self.assertIsNone(self.fixture.store.for_old_session(replacement.OLD_SESSION_ID))
                self.assertEqual([], self.fixture.backend.calls)
                original = output.read_bytes()
                self.assertEqual(0o600, output.stat().st_mode & 0o777)
                summary = json.loads(stdout.getvalue())
                self.assertEqual(digest(original), summary["preparation_sha256"])
                loaded = replacement.load_preparation(original)
                self.assertIsInstance(loaded, Preparation, str(loaded))
                assert isinstance(loaded, Preparation)
                self.assertEqual(self.fixture.inputs.manifest, loaded.manifest)
                self.assertIn(bound_file, loaded.sources)
                self.assertTrue(set(replacement.implementation_sources()).issubset(loaded.sources))
                packet = prepare(loaded, self.fixture.store, self.fixture.backend)
                self.assertIsInstance(packet, ExactPacket, str(packet))
                assert isinstance(packet, ExactPacket)
                self.assertEqual("quoted Source1957 choice", packet.constraints[0].quote)
                self.assertEqual(1, replacement.main(command))
                self.assertEqual(original, output.read_bytes())
            self.assertEqual(self.fixture.task_before, self.fixture.task.read_bytes())
            self.assertEqual(self.fixture.todo_before, self.fixture.todo.read_bytes())

    def test_build_preparation_rejects_bad_manifest_quote_or_output_without_lifecycle(self) -> None:
        from omo_manager import omo_omnigent_server as server

        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            manifest_path, output = private / "manifest.json", private / "preparation.json"
            rows = [{key: value for key, value in asdict(item).items() if key != "queue_item_sha256"} for item in self.fixture.inputs.manifest]
            manifest_path.write_text(replacement.canonical(rows[:-1]))
            kwargs: BuildArguments = {
                "authority": self.fixture.authority,
                "task": self.fixture.task,
                "todo": self.fixture.todo,
                "workspace": self.fixture.workspace,
                "manifest": manifest_path,
                "output": output,
            }
            self.assertEqual("manifest_queue_mismatch", checked(replacement.build_preparation(**kwargs), Rejected).code)
            manifest_path.write_text(replacement.canonical(rows))
            invalid_kwargs = kwargs.copy()
            invalid_kwargs["output"] = self.fixture.root / "forbidden.json"
            self.assertEqual("unsafe_preparation_output", checked(replacement.build_preparation(**invalid_kwargs), Rejected).code)
            constraints = private / "constraints.json"
            constraints.write_text(replacement.canonical([{"source": str(self.fixture.authority), "quote": "not in the authority"}]))
            with patch.object(server, "validate_package", return_value=replacement.OMNIGENT_SOURCE_SHA256):
                self.assertEqual("constraint_quote_mismatch", checked(replacement.build_preparation(**kwargs, constraints=constraints), Rejected).code)
            with patch.object(replacement, "installed_package_digest", return_value=Rejected("installed_package_unverified", "package bytes drifted")):
                self.assertEqual("installed_package_unverified", checked(replacement.build_preparation(**kwargs), Rejected).code)
            self.assertFalse(output.exists())
            self.assertIsNone(self.fixture.store.for_old_session(replacement.OLD_SESSION_ID))
            self.assertEqual([], self.fixture.backend.calls)

    def test_built_preparation_rejects_every_captured_file_drift_before_reservation(self) -> None:
        cases = ("authority", "task", "queue", "todo", "implementation", "backend", "bridge", "host", "local_env", "constraint_source", "manifest", "constraint_list")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                private = Path(directory)
                self.fixture.authority.write_bytes(b"1\nquoted Source1957 choice\n")
                self.fixture.task.write_bytes(self.fixture.task_before)
                self.fixture.todo.write_bytes(self.fixture.todo_before)
                self.fixture.local_config.write_text(f"OMO_SOURCE1957_WORKSPACE={self.fixture.workspace}\n")
                files = {name: private / f"{name}.txt" for name in ("implementation", "backend", "bridge", "host", "constraint_source")}
                for path in files.values():
                    path.write_text("exact reviewed fixture bytes\n")
                manifest = private / "manifest.json"
                manifest.write_text(replacement.canonical([{key: value for key, value in asdict(item).items() if key != "queue_item_sha256"} for item in self.fixture.inputs.manifest]))
                constraints = private / "constraints.json"
                constraints.write_text(replacement.canonical([{"source": str(files["constraint_source"]), "quote": "exact reviewed fixture bytes"}]))
                output = private / "preparation.json"
                result = replacement.build_preparation(
                    authority=self.fixture.authority,
                    task=self.fixture.task,
                    todo=self.fixture.todo,
                    workspace=self.fixture.workspace,
                    manifest=manifest,
                    output=output,
                    constraints=constraints,
                    bound_files=tuple(files[name] for name in ("implementation", "backend", "bridge", "host")),
                )
                self.assertIsInstance(result, dict, str(result))
                assert isinstance(result, dict)
                raw = output.read_bytes()
                bound = replacement.load_preparation(raw)
                self.assertIsInstance(bound, Preparation, str(bound))
                assert isinstance(bound, Preparation)
                assert bound.snapshot is not None
                self.assertEqual(self.fixture.task_before, replacement.decoded(bound.snapshot.task.data_b64))
                self.assertEqual(raw_queue(self.fixture.task_before), replacement.decoded(bound.snapshot.queue_b64))
                target = {
                    "authority": self.fixture.authority,
                    "task": self.fixture.task,
                    "queue": self.fixture.task,
                    "todo": self.fixture.todo,
                    "local_env": self.fixture.local_config,
                    "manifest": manifest,
                    "constraint_list": constraints,
                    **files,
                }[case]
                target.write_bytes(target.read_bytes().replace(b"item 0", b"changed queue item") if case == "queue" else target.read_bytes() + b"changed\n")
                self.assertIsInstance(replacement.load_preparation(raw), Rejected)
                self.assertIsInstance(prepare(bound, self.fixture.store, self.fixture.backend), Rejected)
                self.assertIsNone(self.fixture.store.for_old_session(replacement.OLD_SESSION_ID))
                self.assertEqual([], self.fixture.backend.calls)
                self.assertEqual(raw, output.read_bytes())

    def test_preparation_rejects_workspace_rebind_symlink_ancestor_owner_and_lookalike(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            parent = private / "project"
            parent.mkdir()
            workspace = parent / replacement.WORKSPACE_NAME
            workspace.mkdir()
            manifest = private / "manifest.json"
            manifest.write_text(replacement.canonical([{key: value for key, value in asdict(item).items() if key != "queue_item_sha256"} for item in self.fixture.inputs.manifest]))
            with patch.dict(replacement.LOCAL_ENV, {"OMO_SOURCE1957_WORKSPACE": str(workspace)}):
                output = private / "preparation.json"
                result = replacement.build_preparation(authority=self.fixture.authority, task=self.fixture.task, todo=self.fixture.todo, workspace=workspace, manifest=manifest, output=output)
                self.assertIsInstance(result, dict, str(result))
                assert isinstance(result, dict)
                raw = output.read_bytes()
                self.assertIsInstance(replacement.load_preparation(raw), Preparation)
                lookalike = private / "other" / replacement.WORKSPACE_NAME
                lookalike.mkdir(parents=True)
                rejected = replacement.build_preparation(
                    authority=self.fixture.authority, task=self.fixture.task, todo=self.fixture.todo, workspace=lookalike, manifest=manifest, output=private / "wrong.json"
                )
                self.assertEqual("wrong_preparation_workspace", checked(rejected, Rejected).code)
                actual_lstat = Path.lstat

                def changed_owner(path: Path) -> os.stat_result:
                    original = actual_lstat(path)
                    if path != workspace:
                        return original
                    values = list(original)
                    values[4] = original.st_uid + 1
                    return os.stat_result(values)

                with patch.object(Path, "lstat", changed_owner):
                    rejected = replacement.load_preparation(raw)
                    self.assertIsInstance(rejected, Rejected)
                    assert isinstance(rejected, Rejected)
                    self.assertIn("owned by this user", rejected.detail)
                retired = parent / "retired"
                workspace.rename(retired)
                workspace.mkdir()
                rejected = replacement.load_preparation(raw)
                self.assertIsInstance(rejected, Rejected)
                assert isinstance(rejected, Rejected)
                self.assertIn("workspace, owner or ancestor identities changed", rejected.detail)
                workspace.rmdir()
                retired.rename(workspace)
                self.assertIsInstance(replacement.load_preparation(raw), Preparation)
                saved_parent = private / "saved-project"
                parent.rename(saved_parent)
                parent.symlink_to(saved_parent, target_is_directory=True)
                rejected = replacement.load_preparation(raw)
                self.assertIsInstance(rejected, Rejected)
                assert isinstance(rejected, Rejected)
                self.assertIn("exact canonical existing workspace", rejected.detail)
                self.assertIsNone(self.fixture.store.for_old_session(replacement.OLD_SESSION_ID))

    def test_preparation_package_digest_is_stable_and_old_path_schema_is_rejected(self) -> None:
        bound = replacement.bind_preparation(self.fixture.inputs)
        self.assertIsInstance(bound, Preparation, str(bound))
        assert isinstance(bound, Preparation)
        raw = bound.serialize()
        with patch.object(replacement, "installed_package_digest", return_value=digest(b"changed installed source")):
            self.assertIsInstance(replacement.load_preparation(raw), Rejected)
            self.assertIsInstance(prepare(bound, self.fixture.store, self.fixture.backend), Rejected)
        self.assertIsInstance(replacement.load_preparation(raw), Preparation)
        value = json.loads(raw)
        del value["snapshot"]
        self.assertIsInstance(replacement.load_preparation(replacement.canonical(value).encode()), Rejected)
        self.assertIsNone(self.fixture.store.for_old_session(replacement.OLD_SESSION_ID))

    def test_proven_pre_dispatch_recovery_keeps_one_delivery_and_respects_final_cas(self) -> None:
        packet, approval = self.prepared()
        with patch.object(self.fixture.backend, "deliver", return_value=Rejected("owned_delivery_pre_dispatch", "host staging ended before dispatch")):
            self.assertEqual("owned_delivery_pre_dispatch", checked(execute(packet, approval, self.fixture.store, self.fixture.backend), Rejected).code)
        with (
            patch.object(self.fixture.backend, "inspect_receipt", return_value=Rejected("owned_delivery_pre_dispatch", "durably absent dispatch intent")),
            patch.object(self.fixture.backend, "reconcile_effect", side_effect=lambda value, _phase: self.fixture.backend.deliver(value)) as repair,
        ):
            result = reconcile(packet, approval, self.fixture.store, self.fixture.backend)
        self.assertIsInstance(result, Operation, str(result))
        assert isinstance(result, Operation)
        self.assertEqual("delivery_confirmed", result.phase)
        self.assertEqual(1, self.fixture.backend.calls.count("delivery_confirmed"))
        repair.assert_called_once_with(packet, "delivery_confirmed")

    def test_pre_dispatch_recovery_never_sends_after_intent_or_file_drift(self) -> None:
        for drift in ("intent", "file"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                self.fixture.store = FenceStore(Path(directory) / "fence.sqlite")
                self.fixture.task.write_bytes(self.fixture.task_before)
                self.fixture.todo.write_bytes(self.fixture.todo_before)
                self.fixture.backend = SimulatedLifecycle(self.fixture.runtime)
                packet, approval = self.prepared()
                if drift == "intent":

                    def crash_after_manifest_claim(value: ExactPacket) -> NoReturn:
                        runner_id, thread_id = "test-successor-runner", "test-successor-thread"
                        self.assertIsInstance(self.fixture.store.claim_successor_launch(value.runtime.host_id, value.successor_id, runner_id, "test-launch-request"), Operation)
                        self.assertIsInstance(self.fixture.store.record_evidence(value.operation_id, "native_successor_verified", replacement._evidence(value, thread_id=thread_id)), Operation)
                        self.assertIsInstance(self.fixture.store.claim_manifest_dispatch(value.operation_id, value.delivery_id, digest(value.prompt.encode()), runner_id, thread_id), Operation)
                        raise SimulatedCrash("manifest_dispatch_intent persisted")

                    with patch.object(self.fixture.backend, "deliver", side_effect=crash_after_manifest_claim), self.assertRaises(SimulatedCrash):
                        execute(packet, approval, self.fixture.store, self.fixture.backend)
                else:
                    with patch.object(self.fixture.backend, "deliver", return_value=Rejected("owned_delivery_pre_dispatch", "not sent")):
                        execute(packet, approval, self.fixture.store, self.fixture.backend)
                    self.fixture.task.write_bytes(self.fixture.task.read_bytes() + b"foreign change")
                self.fixture.store = FenceStore(Path(directory) / "fence.sqlite")
                packet = load_packet(packet.serialize())
                self.assertIsInstance(packet, ExactPacket)
                assert isinstance(packet, ExactPacket)
                self.fixture.backend = SimulatedLifecycle(self.fixture.runtime)
                with (
                    patch.object(self.fixture.backend, "inspect_receipt", return_value=Rejected("owned_delivery_pre_dispatch", "claim requires independent checks")),
                    patch.object(self.fixture.backend, "reconcile_effect") as repair,
                ):
                    rejected = reconcile(packet, approval, self.fixture.store, self.fixture.backend)
                    self.assertIsInstance(rejected, Rejected)
                    assert isinstance(rejected, Rejected)
                    if drift == "intent":
                        self.assertEqual("delivery_dispatch_already_intended", checked(rejected, Rejected).code)
                repair.assert_not_called()
                self.assertNotIn("delivery_confirmed", self.fixture.backend.calls)


if __name__ == "__main__":
    unittest.main()
