from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omo_manager.omo_agent_status import read_task_metadata
from omo_manager.omo_task import atomic_replace_if_unchanged
from omo_manager.omo_task import manager_owner_migration_text
from omo_manager.omo_task_metadata import TaskFrontmatterError
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_status import replace_if_unchanged
from omo_manager.omo_task_status import update_frontmatter_status

TASK_ID = "task_019f0000-0000-7000-8000-000000000011"
ITEM_ID = "pi_019f0000-0000-7000-8000-000000000012"


def running_v2(extra: str = "") -> str:
    return f"""---
version: v2.0.0
task_id: {TASK_ID}
status: running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
{extra}pending_task_items: []
resolved_task_items: []
---
body
"""


def blocked_v2(task_reference: str) -> str:
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
  - kind: task
    task: {task_reference}
    reason: waiting for external task
pending_task_items: []
resolved_task_items: []
---
"""


def resolved_v2(resolved_at: str) -> str:
    return running_v2(
        f"""resolved_task_items:
  - id: {ITEM_ID}
    outcome: completed
    evidence: reviewed
    resolved_at: {resolved_at}
    notices: []
"""
    ).replace("resolved_task_items: []\n", "")


def v1_task() -> str:
    return """---
version: v1.0.0
status: running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
pending_task_items: []
---
body
"""


class Phase1MetadataRepairTests(unittest.TestCase):
    def test_nonblocked_v2_rejects_resume_status_even_null_or_empty(self) -> None:
        for value in ("null", "''"):
            with self.subTest(value=value), self.assertRaisesRegex(TaskFrontmatterError, "must only exist"):
                parse_task_metadata(running_v2(f"resume_status: {value}\n"))

    def test_task_blocker_requires_canonical_in_root_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for reference in ("../outside.md", "/tmp/outside.md", "nested//task.md", "nested/./task.md", "nested\\task.md"):
                with self.subTest(reference=reference), self.assertRaisesRegex(TaskFrontmatterError, "canonical relative"):
                    parse_task_metadata(blocked_v2(reference), root)

            metadata = parse_task_metadata(blocked_v2("nested/task.md"), root)

        self.assertIsNotNone(metadata)

    def test_task_blocker_rejects_symlink_escape_from_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / "linked").symlink_to(Path(outside), target_is_directory=True)

            with self.assertRaisesRegex(TaskFrontmatterError, "resolve inside"):
                parse_task_metadata(blocked_v2("linked/task.md"), root)

    def test_yaml_non_string_keys_and_malformed_input_use_frontmatter_error(self) -> None:
        malformed = (
            running_v2("1: value\n"),
            running_v2("? [bad, key]\n: value\n"),
            running_v2().replace("pending_task_items: []", "pending_task_items: ["),
        )
        for text in malformed:
            with self.subTest(text=text), self.assertRaises(TaskFrontmatterError):
                parse_task_metadata(text)

    def test_agent_status_fails_closed_for_malformed_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(running_v2("1: value\n"), encoding="utf-8")

            self.assertIsNone(read_task_metadata(path))

    def test_rejects_iso_basic_datetime_but_accepts_rfc3339(self) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "RFC 3339"):
            parse_task_metadata(resolved_v2("20260725T140000+0000"))

        metadata = parse_task_metadata(resolved_v2("2026-07-25T14:00:00Z"))

        self.assertIsNotNone(metadata)


class Phase1MutationGateTests(unittest.TestCase):
    def test_task_status_transform_supports_v2_after_cli_gate(self) -> None:
        updated = update_frontmatter_status(running_v2(), "blocked", "waiting")

        metadata = parse_task_metadata(updated)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("blocked", metadata.status)
        self.assertEqual("running", metadata.resume_status)
        self.assertEqual("waiting", metadata.blockers[0].reason)  # type: ignore[union-attr]

    def test_task_status_atomic_write_supports_v2_after_cli_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = v1_task()
            path.write_text(original, encoding="utf-8")

            replace_if_unchanged(path, running_v2(), path.stat())

            self.assertEqual(running_v2(), path.read_text(encoding="utf-8"))

    def test_task_owner_migration_supports_validated_v2_yaml(self) -> None:
        updated = manager_owner_migration_text(running_v2(), "wl:1", "wl:3")

        metadata = parse_task_metadata(updated)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("wl:3", metadata.managerat)
        self.assertEqual(TASK_ID, metadata.task_id)

    def test_task_atomic_write_supports_v2_after_cli_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = v1_task()
            path.write_text(original, encoding="utf-8")

            atomic_replace_if_unchanged(path, running_v2(), path.stat())

            self.assertEqual(running_v2(), path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
