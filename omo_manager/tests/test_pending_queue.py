from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_pending import Args
from omo_manager.omo_pending import run
from omo_manager.omo_task_context import infer_active_task


def task_text(status: str = "running", items: tuple[str, ...] = ()) -> str:
    pending = "pending_task_items: []" if not items else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in items)
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        "runat: cfg:2\n"
        "tool: codex\n"
        "managerat: cfg:1\n"
        "is_manager: false\n"
        f"{pending}\n"
        "---\n"
        "work\n"
    )


class PendingQueueTests(unittest.TestCase):
    def test_inference_rejects_current_previous_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n\nprevious:\nold.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")
            (root / "old.md").write_text(task_text(), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_active_task(root, "cfg:2.0")

    def test_inference_accepts_one_long_running_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("current:\ncurrent.md cfg:2\n", encoding="utf-8")
            (root / "current.md").write_text(task_text("long_running"), encoding="utf-8")

            self.assertEqual(root / "current.md", infer_active_task(root, "cfg:2.0"))

    def test_inference_rejects_ambiguous_noncurrent_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "TODO.md").write_text("human pending:\na.md cfg:2\nb.md cfg:2\n", encoding="utf-8")
            (root / "a.md").write_text(task_text(), encoding="utf-8")
            (root / "b.md").write_text(task_text(), encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "multiple active"):
                infer_active_task(root, "cfg:2")

    def test_agent_add_list_replace_remove_is_path_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "secret-task.md"
            (root / "TODO.md").write_text("current:\nsecret-task.md cfg:2\n", encoding="utf-8")
            path.write_text(task_text(), encoding="utf-8")
            output = StringIO()
            with patch("omo_manager.omo_pending.current_active_task", return_value=path), redirect_stdout(output):
                self.assertEqual(0, run(Args("add", ("inspect failure",)), root))
                self.assertEqual(0, run(Args("list"), root))
                self.assertEqual(0, run(Args("replace", old_item="inspect failure", new_item="repair failure"), root))
                self.assertEqual(0, run(Args("remove", ("repair failure",), evidence="verified fixed"), root))
            self.assertNotIn("secret-task", output.getvalue())
            text = path.read_text(encoding="utf-8")
            self.assertIn("verified removed pending item: verified fixed", text)
            self.assertIn("pending_task_items: []", text)


if __name__ == "__main__":
    unittest.main()
