from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path

from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import task_hash
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_task_migrate import Args
from omo_manager.omo_task_migrate import load_plan
from omo_manager.omo_task_migrate import run


def v1_task(*, blocked: bool = False) -> str:
    status = "blocked" if blocked else "running"
    blocked_on = "blocked_on: exact legacy reason\n" if blocked else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocked_on}"
        "runat: cfg:2\n"
        "tool: codex\n"
        "managerat: cfg:1\n"
        "is_manager: false\n"
        "pending_task_items:\n"
        "  - preserve first\n"
        "  - preserve second\n"
        "---\n"
        "body\n"
    )


def commit(root: Path, *paths: str) -> None:
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "test state"],
        cwd=root,
        check=True,
    )


class TaskMigrationV2Tests(unittest.TestCase):
    def test_plan_and_write_are_idempotent_and_preserve_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            plan = root / "migration.yaml"
            original = v1_task(blocked=True)
            task.write_text(original, encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            commit(root, "task.md", "TODO.md")

            self.assertEqual(0, run(Args(root, "plan", plan, (("task.md", "long_running"),))))
            commit(root, "migration.yaml")
            self.assertEqual(0, run(Args(root, "dry-run", plan)))
            self.assertEqual(0, run(Args(root, "write", plan)))
            migrated = task.read_text(encoding="utf-8")
            self.assertEqual(0, run(Args(root, "write", plan)))
            commit(root, "task.md")
            self.assertEqual(0, run(Args(root, "enable", plan)))
            self.assertTrue(v2_enabled(root))
            self.assertEqual(migrated, task.read_text(encoding="utf-8"))
            metadata = parse_task_metadata(migrated)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("preserve first", "preserve second"), metadata.pending_task_items)
            self.assertEqual("long_running", metadata.resume_status)
            self.assertEqual("exact legacy reason", metadata.blockers[0].text)  # type: ignore[union-attr]
            row = load_plan(plan)[0]
            self.assertEqual(task_hash(original), row["v1_sha256"])
            self.assertEqual(task_hash(migrated), row["v2_sha256"])

    def test_plan_rejects_missing_blocked_resume_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.md").write_text(v1_task(blocked=True), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            commit(root, "task.md", "TODO.md")

            with self.assertRaisesRegex(BlockingError, "resume-status"):
                run(Args(root, "plan", root / "migration.yaml"))

    def test_write_rejects_unreviewed_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            plan = root / "migration.yaml"
            task.write_text(v1_task(), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            commit(root, "task.md", "TODO.md")
            self.assertEqual(0, run(Args(root, "plan", plan)))
            commit(root, "migration.yaml")
            task.write_text(v1_task().replace("body", "changed body"), encoding="utf-8")

            with self.assertRaisesRegex(BlockingError, "drifted"):
                run(Args(root, "write", plan))

    def test_write_requires_reviewed_plan_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.md").write_text(v1_task(), encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:2\n", encoding="utf-8")
            commit(root, "task.md", "TODO.md")
            plan = root / "migration.yaml"
            self.assertEqual(0, run(Args(root, "plan", plan)))

            with self.assertRaisesRegex(BlockingError, "migration plan must be clean and committed"):
                run(Args(root, "write", plan))


if __name__ == "__main__":
    unittest.main()
