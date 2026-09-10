from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from omo_manager import omo_config18_satisfied_close as core
from omo_manager.omo_config19_satisfied_close import (
    PENDING_ITEM,
    PROTECTED_TARGETS,
    SCHEMA,
    TARGET,
    TASK,
    UPSTREAM_AUDIT,
    UPSTREAM_SCHEMA,
    configure,
    validate_upstream,
)
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata


class Config19SatisfiedCloseTests(TestCase):
    def test_profile_is_exact_and_core_bound(self) -> None:
        core.AUTHORITY = "poison"
        core.AUTHORITY_SHA256 = "poison"
        core.CURRENT_MANAGER = "poison"
        core.MANAGER_TASK = "poison"
        core.REPLAY_ID = "poison"
        core.SESSION_RE = __import__("re").compile("poison")
        core.SHA256_RE = __import__("re").compile("poison")
        core.PACKET_KEYS = {"poison"}
        with patch("omo_manager.omo_config19_satisfied_close.CORE_SHA256", core.sha256(Path(core.__file__).read_bytes())):
            configure()
        self.assertEqual(SCHEMA, core.SCHEMA)
        self.assertEqual(TARGET, core.TARGET)
        self.assertEqual(TASK, core.TASK)
        self.assertEqual(PROTECTED_TARGETS, core.PROTECTED_TARGETS)
        self.assertEqual("manager_mail/85c5dff58359-1570.txt", core.AUTHORITY)
        self.assertEqual("wl:21", core.CURRENT_MANAGER)
        self.assertEqual("transport_closure_mgr.md", core.MANAGER_TASK)
        self.assertEqual(64, len(core.REPLAY_ID))
        self.assertNotEqual({"poison"}, core.PACKET_KEYS)

    def test_upstream_requires_committed_config18_audit(self) -> None:
        audit = {
            "schema": UPSTREAM_SCHEMA,
            "state": "committed",
            "audit": str(UPSTREAM_AUDIT),
            "task": "config16_close.md",
            "replay_id": core.REPLAY_ID,
            "authority_sha256": core.AUTHORITY_SHA256,
            "manager_target": core.CURRENT_MANAGER,
        }
        validate_upstream(json.dumps(audit).encode())
        audit["task"] = "dw2_input_clear.md"
        with self.assertRaises(TaskFrontmatterError):
            validate_upstream(json.dumps(audit).encode())

    def test_profile_after_images_remove_only_exact_config19_item(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / TASK
            source = f"""---
version: v1.0.0
status: blocked
blocked_on: human
runat: config:19
tool: codex
managerat: wl:21
is_manager: false
pending_task_items:
  - {PENDING_ITEM!r}
---
body
"""
            todo = """current:
human pending:
dw2_wrap_cancel.md config:19
dw2_cleanup_input.md config:20
low priority:
previous:
"""
            task.write_text(source)
            with patch("omo_manager.omo_config19_satisfied_close.CORE_SHA256", core.sha256(Path(core.__file__).read_bytes())):
                configure()
            after_task = core.task_after(root, source).decode()
            after_todo = core.todo_after(root, task, todo).decode()
            metadata = parse_task_metadata(after_task, root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual((), metadata.pending_task_items)
            self.assertIn("dw2_wrap_cancel.md config:19", after_todo)
            self.assertIn("dw2_cleanup_input.md config:20", after_todo)
