from __future__ import annotations

import unittest

from omo_manager.omo_task_metadata import PendingItemsBlocker
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata

TASK_ID = "task_019f0000-0000-7000-8000-000000000001"
OTHER_TASK_ID = "task_019f0000-0000-7000-8000-000000000002"
ITEM_ID = "pi_019f0000-0000-7000-8000-000000000003"
OTHER_ITEM_ID = "pi_019f0000-0000-7000-8000-000000000004"
NOTICE_ID = "wake_019f0000-0000-7000-8000-000000000005"


def v2_task(*, dependency_state: str = "waiting", recipient_task_id: str = TASK_ID, resolved_at: str = "2026-07-25T14:00:00-07:00") -> str:
    return f"""---
version: v2.0.0
task_id: {TASK_ID}
status: blocked
resume_status: running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
blocked_on:
  - kind: pending_items
    item_ids: [{ITEM_ID}]
  - kind: human
    reason: waiting for approval
pending_task_items:
  - id: {ITEM_ID}
    text: integrate reviewed result
    blocked_on:
      - task_id: {OTHER_TASK_ID}
        item_id: {OTHER_ITEM_ID}
        state: {dependency_state}
    notices:
      - id: {NOTICE_ID}
        kind: ready
        state: deferred
        recipient_task_id: {recipient_task_id}
        target_snapshot: wl:2
        attempt_count: 0
        retry_after: null
        escalated_at: null
resolved_task_items:
  - id: pi_019f0000-0000-7000-8000-000000000006
    outcome: completed
    evidence: review passed
    resolved_at: {resolved_at}
    notices: []
---
body
"""


def v1_task() -> str:
    return """---
version: v1.0.0
status: running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
pending_task_items:
  - preserve: exact v1 text
---
"""


class TaskMetadataV2Tests(unittest.TestCase):
    def test_dual_reader_preserves_v1_scalar_item(self) -> None:
        metadata = parse_task_metadata(v1_task())

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(("preserve: exact v1 text",), metadata.pending_task_items)
        self.assertEqual((), metadata.pending_items)

    def test_parses_typed_v2_items_blockers_notices_and_tombstones(self) -> None:
        metadata = parse_task_metadata(v2_task())

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(TASK_ID, metadata.task_id)
        self.assertEqual(("integrate reviewed result",), metadata.pending_task_items)
        self.assertEqual("waiting", metadata.pending_items[0].blocked_on[0].state)
        self.assertEqual(NOTICE_ID, metadata.pending_items[0].notices[0].id)
        self.assertIsInstance(metadata.blockers[0], PendingItemsBlocker)
        self.assertEqual("completed", metadata.resolved_task_items[0].outcome)
        self.assertEqual(-25200, int(metadata.resolved_task_items[0].resolved_at.utcoffset().total_seconds()))

    def test_rejects_non_uuidv7_ids(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "UUIDv7"):
            parse_task_metadata(v2_task().replace(TASK_ID, "task_019f0000-0000-4000-8000-000000000001"))

    def test_rejects_time_without_explicit_offset(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "explicit offset"):
            parse_task_metadata(v2_task(resolved_at="2026-07-25T14:00:00"))

    def test_rejects_notice_for_another_recipient(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "owning task id"):
            parse_task_metadata(v2_task(recipient_task_id=OTHER_TASK_ID))

    def test_accepts_generated_blocker_drift_for_reconciliation(self) -> None:
        text = v2_task().replace(f"item_ids: [{ITEM_ID}]", f"item_ids: [{OTHER_ITEM_ID}]")

        metadata = parse_task_metadata(text)

        self.assertIsNotNone(metadata)
        assert metadata is not None
        blocker = metadata.blockers[0]
        self.assertIsInstance(blocker, PendingItemsBlocker)
        assert isinstance(blocker, PendingItemsBlocker)
        self.assertEqual((OTHER_ITEM_ID,), blocker.item_ids)

if __name__ == "__main__":
    unittest.main()
