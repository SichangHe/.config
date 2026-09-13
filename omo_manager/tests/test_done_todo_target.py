from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from omo_manager.omo_done_todo_target import main


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task(status: str = "done", target: str = "config:24") -> str:
    return (
        "---\nversion: v1.0.0\n"
        f"status: {status}\nrunat: {target}\ntool: codex\nmanagerat: config:27\n"
        "is_manager: false\npending_task_items: []\n---\nevidence\n"
    )


class DoneTodoTargetTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, list[str]]:
        task_path = root / "stale.md"
        task_path.write_text(task(), encoding="utf-8")
        rows = [f"old-{index}.md old:{index}" for index in range(19)]
        todo = root / "TODO.md"
        todo.write_text(
            "current:\nlive.md config:24\n\nhuman pending:\n\nlow priority:\n\nprevious:\n"
            + "\n".join(["stale.md", *rows])
            + "\n",
            encoding="utf-8",
        )
        return task_path, todo, rows

    def invoke(self, root: Path, task_path: Path, todo: Path, **overrides: str) -> int:
        values = {
            "task": task_path.name,
            "target": "config:24",
            "task_sha256": digest(task_path),
            "todo_sha256": digest(todo),
            "expected_previous_count": "20",
            **overrides,
        }
        return main(
            [
                "--root",
                str(root),
                "--task",
                values["task"],
                "--target",
                values["target"],
                "--task-sha256",
                values["task_sha256"],
                "--todo-sha256",
                values["todo_sha256"],
                "--expected-previous-count",
                values["expected_previous_count"],
            ]
        )

    def test_restores_only_done_previous_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, todo, rows = self.fixture(root)
            before = task_path.read_bytes()
            self.assertEqual(0, self.invoke(root, task_path, todo))
            self.assertEqual(before, task_path.read_bytes())
            self.assertEqual(
                "current:\nlive.md config:24\n\nhuman pending:\n\nlow priority:\n\nprevious:\n"
                + "\n".join(["stale.md config:24", *rows])
                + "\n",
                todo.read_text(encoding="utf-8"),
            )

    def test_rejects_changed_inputs_and_wrong_lifecycle(self) -> None:
        for case in ("task-digest", "todo-digest", "running", "wrong-target", "wrong-count"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task_path, todo, _rows = self.fixture(root)
                overrides: dict[str, str] = {}
                if case == "task-digest":
                    overrides["task_sha256"] = "0" * 64
                elif case == "todo-digest":
                    overrides["todo_sha256"] = "0" * 64
                elif case == "running":
                    task_path.write_text(task("running"), encoding="utf-8")
                elif case == "wrong-target":
                    overrides["target"] = "config:25"
                else:
                    overrides["expected_previous_count"] = "19"
                before = todo.read_bytes()
                self.assertEqual(2, self.invoke(root, task_path, todo, **overrides))
                self.assertEqual(before, todo.read_bytes())

    def test_rejects_non_previous_or_already_targeted_row(self) -> None:
        for replacement in ("stale.md config:24", ""):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task_path, todo, _rows = self.fixture(root)
                text = todo.read_text(encoding="utf-8").replace("stale.md\n", f"{replacement}\n", 1)
                todo.write_text(text, encoding="utf-8")
                before = todo.read_bytes()
                self.assertEqual(2, self.invoke(root, task_path, todo))
                self.assertEqual(before, todo.read_bytes())

    def test_rejects_noncanonical_zero_pane_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, todo, _rows = self.fixture(root)
            before = todo.read_bytes()
            with self.assertRaises(SystemExit):
                _ = self.invoke(root, task_path, todo, target="config:24.0")
            self.assertEqual(before, todo.read_bytes())


if __name__ == "__main__":
    unittest.main()
