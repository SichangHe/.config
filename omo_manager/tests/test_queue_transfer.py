from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_replace import create_snapshot as real_create_snapshot
from omo_manager.omo_manager_replace import replace_snapshot as real_replace_snapshot
from omo_manager.omo_queue_transfer import (
    BoundArgs,
    PrepareArgs,
    QueueTransferError,
    REQUEST_SCHEMA,
    apply_transfer,
    canonical_bytes,
    digest,
    prepare_transfer,
    queue_digest,
    verify_transfer,
)
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_lock import task_target_lock


def task_text(target: str, items: tuple[str, ...], body: str) -> str:
    pending = "pending_task_items: []\n" if not items else "pending_task_items:\n" + "".join(f"  - {json.dumps(item)}\n" for item in items)
    return f"---\nversion: v1.0.0\nstatus: running\nrunat: {target}\ntool: codex\nmanagerat: mgr:0\nis_manager: false\n{pending}---\n{body}\n"


def write_task(root: Path, name: str, target: str, items: tuple[str, ...]) -> Path:
    path = root / name
    path.write_text(task_text(target, items, name), encoding="utf-8")
    return path


def request_data(source: Path, source_items: tuple[str, ...], destinations: tuple[Path, ...], dispositions: list[dict[str, object]]) -> bytes:
    return canonical_bytes(
        {
            "schema": REQUEST_SCHEMA,
            "source_task": source.name,
            "source_task_sha256": digest(source.read_bytes()),
            "source_queue": list(source_items),
            "source_queue_sha256": queue_digest(source_items),
            "destinations": [{"task": destination.name, "task_sha256": digest(destination.read_bytes())} for destination in destinations],
            "dispositions": dispositions,
        }
    )


def write_request(root: Path, data: bytes) -> tuple[Path, str, Path]:
    request = root / "request.json"
    request.write_bytes(data)
    request.chmod(0o600)
    request_sha256 = digest(data)
    return request, request_sha256, root / f".omo-queue-transfer-{request_sha256}.json"


class QueueTransferTests(unittest.TestCase):
    def test_complete_queue_transfer_commits_ordered_manifest_and_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("first request", "obsolete duplicate", "last request")
            source = write_task(root, "source.md", "src:0", source_items)
            first = write_task(root, "first.md", "dst:1", ("existing first",))
            second = write_task(root, "second.md", "dst:2", ("existing second",))
            data = request_data(
                source,
                source_items,
                (first, second),
                [
                    {"source_index": 0, "kind": "transfer", "destination": "second.md", "evidence": ""},
                    {"source_index": 1, "kind": "duplicate", "destination": "", "evidence": "already owned by the retained task"},
                    {"source_index": 2, "kind": "transfer", "destination": "first.md", "evidence": ""},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)

            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            source_before_apply = source.read_bytes()
            committed = apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))
            plan = verify_transfer(BoundArgs(root, manifest_path, digest(committed.data)))

            source_metadata = parse_task_metadata(source.read_text(encoding="utf-8"), root)
            first_metadata = parse_task_metadata(first.read_text(encoding="utf-8"), root)
            second_metadata = parse_task_metadata(second.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(source_metadata)
            self.assertIsNotNone(first_metadata)
            self.assertIsNotNone(second_metadata)
            assert source_metadata is not None and first_metadata is not None and second_metadata is not None
            self.assertEqual((), source_metadata.pending_task_items)
            self.assertEqual(("existing first", "last request"), first_metadata.pending_task_items)
            self.assertEqual(("existing second", "first request"), second_metadata.pending_task_items)
            self.assertIn("already owned by the retained task", source.read_text(encoding="utf-8"))
            self.assertNotEqual(source_before_apply, source.read_bytes())
            self.assertEqual(source_items, plan.source.before_queue)
            self.assertEqual(2, len(plan.destinations))
            self.assertTrue(all(destination.receipt_path.is_file() for destination in plan.destinations))
            self.assertEqual("committed", json.loads(committed.data)["phase"])
            for destination in plan.destinations:
                receipt = json.loads(destination.receipt_path.read_bytes())
                self.assertEqual(digest(committed.data), receipt["committed_manifest_sha256"])

    def test_retry_recovers_after_source_relinquishes_before_destination_acquires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ())
            data = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "cancelled", "destination": "", "evidence": "request was withdrawn"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            crashed = False

            def clear_then_crash(snapshot: object, replacement: bytes, label: str):
                nonlocal crashed
                result = real_replace_snapshot(snapshot, replacement, label)  # type: ignore[arg-type]
                if label == "source task" and not crashed:
                    crashed = True
                    raise QueueTransferError("simulated interruption after source relinquished")
                return result

            with patch("omo_manager.omo_queue_transfer.replace_snapshot", side_effect=clear_then_crash), self.assertRaisesRegex(QueueTransferError, "simulated interruption"):
                apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))

            self.assertEqual((), parse_task_metadata(source.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]
            self.assertEqual((), parse_task_metadata(destination.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]
            self.assertEqual(0, len(list(root.glob("*.receipt.json"))))
            with self.assertRaisesRegex(QueueTransferError, "not committed"):
                verify_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))
            committed = apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))
            _ = verify_transfer(BoundArgs(root, manifest_path, digest(committed.data)))
            self.assertEqual((), parse_task_metadata(source.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]
            self.assertEqual(("request one",), parse_task_metadata(destination.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]

    def test_retry_with_prepared_digest_recovers_receipts_after_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ())
            data = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "cancelled", "destination": "", "evidence": "withdrawn"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            prepared_sha256 = digest(prepared.data)
            crashed = False

            def commit_then_crash(snapshot: object, replacement: bytes, label: str):
                nonlocal crashed
                result = real_replace_snapshot(snapshot, replacement, label)  # type: ignore[arg-type]
                if label == "queue-transfer manifest" and not crashed:
                    crashed = True
                    raise QueueTransferError("simulated interruption after manifest commit")
                return result

            with patch("omo_manager.omo_queue_transfer.replace_snapshot", side_effect=commit_then_crash), self.assertRaisesRegex(QueueTransferError, "simulated interruption"):
                apply_transfer(BoundArgs(root, manifest_path, prepared_sha256))

            self.assertEqual("committed", json.loads(manifest_path.read_bytes())["phase"])
            self.assertEqual(0, len(list(root.glob("*.receipt.json"))))
            committed = apply_transfer(BoundArgs(root, manifest_path, prepared_sha256))
            _ = verify_transfer(BoundArgs(root, manifest_path, digest(committed.data)))
            self.assertEqual(1, len(list(root.glob("*.receipt.json"))))

    def test_retry_recovers_partial_receipt_publication_after_manifest_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            first = write_task(root, "first.md", "dst:1", ())
            second = write_task(root, "second.md", "dst:2", ())
            data = request_data(
                source,
                source_items,
                (first, second),
                [
                    {"source_index": 0, "kind": "transfer", "destination": first.name, "evidence": ""},
                    {"source_index": 1, "kind": "transfer", "destination": second.name, "evidence": ""},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            prepared_sha256 = digest(prepared.data)
            crashed = False

            def receipt_then_crash(path: Path, replacement: bytes, mode: int):
                nonlocal crashed
                result = real_create_snapshot(path, replacement, mode)
                if path.name.endswith(".receipt.json") and not crashed:
                    crashed = True
                    raise QueueTransferError("simulated interruption after first receipt")
                return result

            with patch("omo_manager.omo_queue_transfer.create_snapshot", side_effect=receipt_then_crash), self.assertRaisesRegex(
                QueueTransferError, "simulated interruption"
            ):
                apply_transfer(BoundArgs(root, manifest_path, prepared_sha256))

            self.assertEqual("committed", json.loads(manifest_path.read_bytes())["phase"])
            self.assertEqual(1, len(list(root.glob("*.receipt.json"))))
            committed = apply_transfer(BoundArgs(root, manifest_path, prepared_sha256))
            plan = verify_transfer(BoundArgs(root, manifest_path, digest(committed.data)))
            self.assertEqual(2, len(list(root.glob("*.receipt.json"))))
            self.assertTrue(all(json.loads(item.receipt_path.read_bytes())["committed_manifest_sha256"] == digest(committed.data) for item in plan.destinations))

    def test_apply_rejects_unknown_destination_bytes_without_clearing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ())
            data = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "completed", "destination": "", "evidence": "verified complete"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            destination.write_text(task_text("dst:0", ("concurrent item",), destination.name), encoding="utf-8")

            with self.assertRaisesRegex(QueueTransferError, "unknown bytes"):
                apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))

            metadata = parse_task_metadata(source.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(source_items, metadata.pending_task_items)
            self.assertEqual([], list(root.glob("*.receipt.json")))

    def test_apply_holds_supported_target_locks_across_the_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ())
            data = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "completed", "destination": "", "evidence": "verified complete"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))

            with task_target_lock(root, "dst:0"), patch("omo_manager.omo_queue_transfer.LOCK_TIMEOUT_S", 0):
                with self.assertRaisesRegex(TimeoutError, "timed out acquiring task-file lock"):
                    apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))

            self.assertEqual(source_items, parse_task_metadata(source.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]
            self.assertEqual((), parse_task_metadata(destination.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]

    def test_prepare_rejects_duplicate_or_human_owner_targets(self) -> None:
        for source_target, destination_target, error in (
            ("src:0", "src:0.0", "distinct owner targets"),
            ("hwork:0", "dst:0", "human-owned"),
        ):
            with self.subTest(source_target=source_target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                source_items = ("request one", "request two")
                source = write_task(root, "source.md", source_target, source_items)
                destination = write_task(root, "destination.md", destination_target, ())
                data = request_data(
                    source,
                    source_items,
                    (destination,),
                    [
                        {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                        {"source_index": 1, "kind": "completed", "destination": "", "evidence": "verified complete"},
                    ],
                )
                request, request_sha256, manifest_path = write_request(root, data)
                with self.assertRaisesRegex(QueueTransferError, error):
                    prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))

    def test_apply_rejects_precommit_receipt_without_mutating_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ())
            data = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "completed", "destination": "", "evidence": "verified complete"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, data)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            receipt = root / f".omo-queue-transfer-{request_sha256}.{digest(destination.name.encode())[:16]}.receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(QueueTransferError, "receipt exists before"):
                apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))

            self.assertEqual(source_items, parse_task_metadata(source.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]
            self.assertEqual((), parse_task_metadata(destination.read_text(encoding="utf-8"), root).pending_task_items)  # type: ignore[union-attr]

    def test_prepare_rejects_incomplete_dispositions_and_existing_destination_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("request one", "request two")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ("request one",))
            incomplete = request_data(
                source,
                source_items,
                (destination,),
                [{"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""}],
            )
            request, request_sha256, manifest_path = write_request(root, incomplete)
            with self.assertRaisesRegex(QueueTransferError, "every source item"):
                prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))

            complete = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "cancelled", "destination": "", "evidence": "withdrawn"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, complete)
            with self.assertRaisesRegex(QueueTransferError, "already contains a source item"):
                prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))

    def test_duplicate_source_text_requires_terminal_duplicate_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_items = ("same request", "same request")
            source = write_task(root, "source.md", "src:0", source_items)
            destination = write_task(root, "destination.md", "dst:0", ())
            duplicate_transfer = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "transfer", "destination": destination.name, "evidence": ""},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, duplicate_transfer)
            with self.assertRaisesRegex(QueueTransferError, "may transfer once"):
                prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))

            one_transfer = request_data(
                source,
                source_items,
                (destination,),
                [
                    {"source_index": 0, "kind": "transfer", "destination": destination.name, "evidence": ""},
                    {"source_index": 1, "kind": "duplicate", "destination": "", "evidence": "same text appears twice"},
                ],
            )
            request, request_sha256, manifest_path = write_request(root, one_transfer)
            prepared = prepare_transfer(PrepareArgs(root, request, request_sha256, manifest_path))
            committed = apply_transfer(BoundArgs(root, manifest_path, digest(prepared.data)))
            _ = verify_transfer(BoundArgs(root, manifest_path, digest(committed.data)))
            metadata = parse_task_metadata(destination.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("same request",), metadata.pending_task_items)


if __name__ == "__main__":
    unittest.main()
