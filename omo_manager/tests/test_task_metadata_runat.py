from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskLine, classify_task
from omo_manager.omo_task_metadata import TARGET_RE, TaskFrontmatterError, canonical_target, parse_task_metadata, runat_kind
from omo_manager.omo_tmux_send import CodexSendOptions, send_to_codex
from omo_manager.omo_task_status import main as task_status_main


def task(version: str, runat: str) -> str:
    v2_fields = (
        """task_id: task_019f0000-0000-7000-8000-000000000001
resolved_task_items: []
"""
        if version == "v2.0.0"
        else ""
    )
    return f"""---
version: {version}
status: running
runat: {runat}
tool: omnigent
managerat: manager:1
is_manager: false
{v2_fields}pending_task_items: []
---
"""


class TaskMetadataRunatTests(unittest.TestCase):
    def test_v1_and_v2_accept_omnigent_session_runat(self) -> None:
        for version in ("v1.0.0", "v2.0.0"):
            with self.subTest(version=version):
                metadata = parse_task_metadata(task(version, "omnigent://019f0000-0000-7000-8000-000000000123"))
                assert metadata is not None
                self.assertEqual("omnigent://019f0000-0000-7000-8000-000000000123", metadata.runat)
                self.assertEqual("omnigent", runat_kind(metadata.runat))

    def test_tmux_targets_keep_precedence_and_canonicalization(self) -> None:
        for target, canonical in (("wl:2", "wl:2"), ("wl:2.0", "wl:2"), ("omnigent:123", "omnigent:123")):
            with self.subTest(target=target):
                self.assertEqual("tmux", runat_kind(target))
                self.assertEqual(canonical, canonical_target(target))
                self.assertIsNotNone(TARGET_RE.fullmatch(target))

    def test_omnigent_session_suffix_is_not_tmux_canonicalized(self) -> None:
        target = "omnigent://session.0"
        self.assertEqual("omnigent", runat_kind(target))
        self.assertEqual(target, canonical_target(target))
        self.assertIsNone(TARGET_RE.search(target))

    def test_rejects_malformed_omnigent_runat(self) -> None:
        for target in ("omnigent://", "omnigent://../session", "omnigent://session/id", "omnigent:session-123", "other://session-123"):
            with self.subTest(target=target), self.assertRaisesRegex(TaskFrontmatterError, "omnigent://SESSION_ID"):
                _ = parse_task_metadata(task("v1.0.0", target))

    def test_managerat_remains_tmux_only(self) -> None:
        text = task("v1.0.0", "wl:2").replace("managerat: manager:1", "managerat: omnigent://manager-session")
        with self.assertRaisesRegex(TaskFrontmatterError, "managerat.*tmux"):
            _ = parse_task_metadata(text)

    def test_omnigent_runat_requires_matching_tool(self) -> None:
        text = task("v1.0.0", "omnigent://session-123").replace("tool: omnigent", "tool: codex")
        with self.assertRaisesRegex(TaskFrontmatterError, "requires `tool: omnigent`"):
            _ = parse_task_metadata(text)

    def test_agent_status_does_not_inspect_omnigent_as_tmux(self) -> None:
        target = "omnigent://session-123"
        task_line = TaskLine("task.md", "task-file", "", target, None, "running")
        with patch("omo_manager.omo_agent_status.inspect") as inspect:
            row = classify_task(task_line, None, auto_unstick=True)
        inspect.assert_not_called()
        self.assertEqual("error", row.status)
        self.assertIn("status_adapter=unsupported", row.evidence)

    def test_tmux_delivery_rejects_omnigent_before_side_effects(self) -> None:
        with patch("omo_manager.omo_tmux_send.write_private_temp") as write_temp, self.assertRaisesRegex(RuntimeError, "requires a tmux target"):
            send_to_codex("omnigent://session-123", "message", CodexSendOptions(1, 0, False))
        write_temp.assert_not_called()

    def test_task_status_rejects_omnigent_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "task.md"
            original = task("v1.0.0", "omnigent://session-123")
            _ = task_path.write_text(original, encoding="utf-8")
            self.assertEqual(2, task_status_main(["--root", str(root), "task.md", "blocked", "--blocked-on", "waiting"]))
            self.assertEqual(original, task_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    _ = unittest.main()
