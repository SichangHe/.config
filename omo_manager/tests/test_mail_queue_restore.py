from __future__ import annotations

# pyright: reportUninitializedInstanceVariable=false

import hashlib
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import final
from typing import override
from unittest.mock import patch

from omo_manager import omo_mail_queue_restore as restore
from omo_manager.omo_task_metadata import parse_task_metadata


def task(items: tuple[str, ...], *, body: str = "preserved body\n", status: str = "running") -> bytes:
    blocked = "blocked_on: preserved blocker\n" if status == "blocked" else ""
    queue = "".join(f"  - '{item}'\n" for item in items)
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocked}"
        "runat: wl:123\n"
        "tool: codex\n"
        "managerat: pb:1\n"
        "is_manager: false\n"
        "pending_task_items:\n"
        f"{queue}"
        "session_id: 01a099b9-0e39-7081-b5de-a3708e44275d\n"
        "---\n"
        f"{body}"
    ).encode()


def item(item_id: str, number: int, *, kind: str = "total-cleanup") -> str:
    return f"email_idle_watcher {kind} threshold {item_id}: retained manager mail {number} exceeds 29"


@final
class MailQueueRestoreTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]
    root: Path
    historical_items: tuple[str, ...]
    prior: bytes
    path: Path
    commit: str
    current_items: tuple[str, ...]
    current: bytes
    patches: ExitStack

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        _ = subprocess.run(["git", "init", "-q", "-b", "main", str(self.root)], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "config", "user.name", "test"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        self.historical_items = tuple(item(f"{number:032x}", number) for number in range(47))
        self.prior = task(self.historical_items, body="historical body\n")
        self.path = self.root / restore.TASK_NAME
        _ = self.path.write_bytes(self.prior)
        _ = subprocess.run(["git", "-C", str(self.root), "add", restore.TASK_NAME], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "prior"], check=True)
        self.commit = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        self.current_items = (item("e" * 32, 64), item("f" * 32, 65))
        self.current = task(self.current_items, body="later bytes stay exact\n", status="blocked")
        _ = self.path.write_bytes(self.current)
        self.patches = ExitStack()
        _ = self.patches.enter_context(patch.object(restore, "PRIOR_COMMIT", self.commit))
        _ = self.patches.enter_context(patch.object(restore, "PRIOR_TASK_SHA256", hashlib.sha256(self.prior).hexdigest()))
        _ = self.patches.enter_context(patch.object(restore, "RECONCILED_NEWEST_IDS", (f"{45:032x}", f"{46:032x}")))
        _ = self.patches.enter_context(patch.object(restore, "CURRENT_ITEMS", self.current_items))

    @override
    def tearDown(self) -> None:
        self.patches.close()
        self.temporary.cleanup()

    def run_restore(self) -> restore.RestoreResult:
        return restore.restore(self.root, hashlib.sha256(self.current).hexdigest())

    def test_success_restores_exact_subset_and_preserves_order_and_later_bytes(self) -> None:
        result = self.run_restore()
        updated = self.path.read_bytes()
        metadata = parse_task_metadata(updated.decode(), self.root)

        self.assertIsNotNone(metadata)
        if metadata is None:
            raise AssertionError("restored metadata is missing")
        self.assertEqual((*self.historical_items[:45], *self.current_items), metadata.pending_task_items)
        self.assertNotIn(self.historical_items[45].encode(), updated)
        self.assertNotIn(self.historical_items[46].encode(), updated)
        self.assertTrue(updated.endswith(b"later bytes stay exact\n"))
        self.assertEqual(tuple(f"{number:032x}" for number in range(45)), result.restored_item_ids)
        self.assertEqual((f"{45:032x}", f"{46:032x}"), result.reconciled_item_ids)
        self.assertEqual(("e" * 32, "f" * 32), result.current_item_ids)
        self.assertEqual(hashlib.sha256(updated).hexdigest(), result.restored_sha256)

    def test_digest_path_history_and_item_set_mismatches_write_nothing(self) -> None:
        cases = ("current digest", "prior digest", "path", "history", "historical count", "reconciled set", "current subset", "current superset", "current overlap")
        for case in cases:
            with self.subTest(case=case):
                original = self.path.read_bytes()
                if case == "current digest":
                    expected = "0" * 64
                else:
                    expected = hashlib.sha256(original).hexdigest()
                if case == "path":
                    self.path.unlink()
                    _ = (self.root / "other.md").write_bytes(original)
                    self.path.symlink_to("other.md")
                elif case == "current overlap":
                    changed = task((self.historical_items[0],), status="blocked")
                    _ = self.path.write_bytes(changed)
                    original = changed
                    expected = hashlib.sha256(changed).hexdigest()
                elif case == "current subset":
                    changed = task(self.current_items[:1], status="blocked")
                    _ = self.path.write_bytes(changed)
                    original = changed
                    expected = hashlib.sha256(changed).hexdigest()
                elif case == "current superset":
                    changed = task((*self.current_items, item("d" * 32, 66)), status="blocked")
                    _ = self.path.write_bytes(changed)
                    original = changed
                    expected = hashlib.sha256(changed).hexdigest()
                with ExitStack() as stack:
                    if case == "prior digest":
                        _ = stack.enter_context(patch.object(restore, "PRIOR_TASK_SHA256", "0" * 64))
                    elif case == "history":
                        _ = stack.enter_context(patch.object(restore, "PRIOR_COMMIT", "0" * 40))
                    elif case == "historical count":
                        _ = stack.enter_context(patch.object(restore, "N_HISTORICAL_ITEMS", 48))
                    elif case == "reconciled set":
                        _ = stack.enter_context(patch.object(restore, "RECONCILED_NEWEST_IDS", ("0" * 32, "1" * 32)))
                    with self.assertRaises((OSError, restore.RestoreError, subprocess.SubprocessError)):
                        _ = restore.restore(self.root, expected)
                if case == "path":
                    self.assertTrue(self.path.is_symlink())
                    self.path.unlink()
                    _ = self.path.write_bytes(original)
                    (self.root / "other.md").unlink()
                else:
                    self.assertEqual(original, self.path.read_bytes())
                _ = self.path.write_bytes(self.current)

    def test_concurrent_change_wins_and_exact_cas_refuses_restore(self) -> None:
        concurrent = self.current + b"concurrent later bytes\n"
        real_atomic = restore.atomic_replace_locked

        def mutate_then_replace(path: Path, payload: bytes, before: os.stat_result, expected: bytes) -> None:
            _ = path.write_bytes(concurrent)
            real_atomic(path, payload, before, expected)

        with patch.object(restore, "atomic_replace_locked", side_effect=mutate_then_replace), self.assertRaises(restore.RestoreError):
            _ = self.run_restore()
        self.assertEqual(concurrent, self.path.read_bytes())
        self.assertEqual([], list(self.root.glob(f".{restore.TASK_NAME}.*")))

    def test_supported_concurrent_restorations_serialize_and_only_one_writes(self) -> None:
        barrier = threading.Barrier(2)

        def run_one(_index: int) -> str:
            _ = barrier.wait(timeout=5)
            try:
                _ = self.run_restore()
            except restore.RestoreError as error:
                return str(error)
            return "success"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(run_one, range(2)))
        self.assertEqual(1, results.count("success"))
        self.assertEqual(1, sum("digest does not match" in result or "current item set is invalid" in result for result in results))
        metadata = parse_task_metadata(self.path.read_text(), self.root)
        self.assertIsNotNone(metadata)
        if metadata is None:
            raise AssertionError("restored metadata is missing")
        self.assertEqual((*self.historical_items[:45], *self.current_items), metadata.pending_task_items)

    def test_duplicate_item_set_is_rejected_before_writing(self) -> None:
        duplicate = task((self.current_items[0], self.current_items[0]), status="blocked")
        _ = self.path.write_bytes(duplicate)
        with self.assertRaises(restore.RestoreError):
            _ = restore.restore(self.root, hashlib.sha256(duplicate).hexdigest())
        self.assertEqual(duplicate, self.path.read_bytes())

    def test_prepublication_failure_has_no_partial_write_or_residue(self) -> None:
        with patch("omo_manager.omo_mail_queue_restore.os.replace", side_effect=OSError("forced replace failure")), self.assertRaises(OSError):
            _ = self.run_restore()
        self.assertEqual(self.current, self.path.read_bytes())
        self.assertEqual([], list(self.root.glob(f".{restore.TASK_NAME}.*")))


if __name__ == "__main__":
    _ = unittest.main()
