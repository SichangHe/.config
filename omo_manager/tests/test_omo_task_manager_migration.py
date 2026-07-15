import contextlib
import fcntl
import io
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_task import main, manager_owner_migration_text, migrate_manager_owner, parse_args


TASK_TEXT = (
    "---\r\n"
    "version: v1.0.0\r\n"
    "status: blocked\r\n"
    "blocked_on: waiting for a dependency\r\n"
    "runat: worker:7.0\r\n"
    "tool: pcodx\r\n"
    "managerat:\told:1 \t\r\n"
    "is_manager: false\r\n"
    "pending_task_items:\r\n"
    "  - preserve first item\r\n"
    "  - preserve second item\r\n"
    "---\r\n"
    "Task body keeps spacing and a body-only owner mention.  \r\n"
    "managerat: old:1\r\n"
)


class ManagerOwnerMigrationTests(unittest.TestCase):
    def migration_argv(self, root: Path, old_owner: str = "old:1", new_owner: str = "new:2.3") -> list[str]:
        return [
            "--root",
            str(root),
            "--task-file",
            "x.md",
            "--migrate-manager-owner",
            "--old-manager-target",
            old_owner,
            "--new-manager-target",
            new_owner,
        ]

    def write_fixture(self, root: Path, text: str = TASK_TEXT) -> tuple[Path, Path, bytes]:
        task = root / "x.md"
        task.write_bytes(text.encode())
        todo = root / "TODO.md"
        todo_bytes = b"current:\r\nx.md worker:7.0 (blocked: keep this byte-for-byte)\r\n"
        todo.write_bytes(todo_bytes)
        return task, todo, todo_bytes

    def assert_main_refuses_without_mutation(self, text: str, old_owner: str = "old:1", new_owner: str = "new:2.3") -> str:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root, text)
            original = task.read_bytes()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), patch("omo_manager.omo_task.tmux") as tmux:
                self.assertEqual(1, main(self.migration_argv(root, old_owner, new_owner)))
            self.assertEqual(original, task.read_bytes())
            self.assertEqual(todo_bytes, todo.read_bytes())
            tmux.assert_not_called()
            return stderr.getvalue()

    def test_migration_atomically_changes_only_frontmatter_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            task.chmod(0o640)
            original = task.read_bytes()
            original_inode = task.stat().st_ino
            expected = original.replace(b"managerat:\told:1 \t", b"managerat:\tnew:2.3 \t", 1)
            stdout = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                patch("omo_manager.omo_task.new_window") as new_window,
                patch("omo_manager.omo_task.link_todo") as link_todo,
                patch("omo_manager.omo_task.start_codex") as start_codex,
                patch("omo_manager.omo_task.tmux") as tmux,
                patch("omo_manager.omo_task.os.replace", wraps=os.replace) as replace,
            ):
                self.assertEqual(0, main(self.migration_argv(root)))

            self.assertEqual(expected, task.read_bytes())
            self.assertEqual(todo_bytes, todo.read_bytes())
            self.assertEqual(0o640, task.stat().st_mode & 0o777)
            self.assertNotEqual(original_inode, task.stat().st_ino)
            self.assertEqual([], list(root.glob(".x.md.*")))
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("blocked", metadata.status)
            self.assertEqual("waiting for a dependency", metadata.blocked_on)
            self.assertEqual("worker:7.0", metadata.runat)
            self.assertEqual("pcodx", metadata.tool)
            self.assertEqual("new:2.3", metadata.managerat)
            self.assertFalse(metadata.is_manager)
            self.assertEqual(("preserve first item", "preserve second item"), metadata.pending_task_items)
            self.assertIn(b"managerat: old:1\r\n", task.read_bytes())
            self.assertIn("migrated only managerat", stdout.getvalue())
            replace.assert_called_once()
            new_window.assert_not_called()
            link_todo.assert_not_called()
            start_codex.assert_not_called()
            tmux.assert_not_called()

    def test_migration_repairs_legacy_manager_self_ownership(self) -> None:
        legacy = TASK_TEXT.replace("runat: worker:7.0", "runat: old:1").replace("is_manager: false", "is_manager: true")
        updated = manager_owner_migration_text(legacy, "old:1", "new:2.3")

        metadata = parse_task_metadata(updated)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("old:1", metadata.runat)
        self.assertEqual("new:2.3", metadata.managerat)
        self.assertTrue(metadata.is_manager)

    def test_legacy_self_owner_repair_still_rejects_other_invalid_metadata(self) -> None:
        legacy = TASK_TEXT.replace("runat: worker:7.0", "runat: old:1").replace("status: blocked", "status: waiting")

        with self.assertRaisesRegex(ValueError, "status"):
            _ = manager_owner_migration_text(legacy, "old:1", "new:2.3")

    def test_dry_run_validates_but_mutates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            original = task.read_bytes()
            original_stat = task.stat()
            stdout = io.StringIO()
            with (
                contextlib.redirect_stdout(stdout),
                patch("omo_manager.omo_task.os.replace") as replace,
                patch("omo_manager.omo_task.tmux") as tmux,
                patch("omo_manager.omo_task.link_todo") as link_todo,
            ):
                self.assertEqual(0, main([*self.migration_argv(root), "--dry-run"]))
            self.assertEqual(original, task.read_bytes())
            self.assertEqual(original_stat, task.stat())
            self.assertEqual(todo_bytes, todo.read_bytes())
            self.assertIn("no files or tmux panes changed", stdout.getvalue())
            replace.assert_not_called()
            tmux.assert_not_called()
            link_todo.assert_not_called()

    def test_rejects_missing_task_without_creating_or_linking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), patch("omo_manager.omo_task.tmux") as tmux:
                self.assertEqual(1, main(self.migration_argv(root)))
            self.assertIn("requires an existing task file", stderr.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())
            tmux.assert_not_called()

    def test_rejects_missing_owner_arguments_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            original = task.read_bytes()
            for omitted in ("--old-manager-target", "--new-manager-target"):
                with self.subTest(omitted=omitted):
                    argv = self.migration_argv(root)
                    index = argv.index(omitted)
                    del argv[index : index + 2]
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        _ = main(argv)
                    self.assertEqual(original, task.read_bytes())
                    self.assertEqual(todo_bytes, todo.read_bytes())

    def test_rejects_owner_arguments_without_deliberate_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            argv = [item for item in self.migration_argv(root) if item != "--migrate-manager-owner"]
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                _ = main(argv)
            self.assertEqual(TASK_TEXT.encode(), task.read_bytes())
            self.assertEqual(todo_bytes, todo.read_bytes())

    def test_rejects_malformed_owner_targets(self) -> None:
        for old_owner, new_owner in (("old", "new:2"), ("old:1", "new"), ("old:1 extra", "new:2"), ("old:1", "2:3")):
            with self.subTest(old_owner=old_owner, new_owner=new_owner):
                error = self.assert_main_refuses_without_mutation(TASK_TEXT, old_owner, new_owner)
                self.assertIn("full tmux target", error)

    def test_rejects_same_or_alias_ambiguous_owners(self) -> None:
        for new_owner in ("old:1", "old:1.0", "old:01.00"):
            with self.subTest(new_owner=new_owner):
                error = self.assert_main_refuses_without_mutation(TASK_TEXT, new_owner=new_owner)
                self.assertIn("different tmux panes", error)

    def test_rejects_stale_old_owner_including_alias(self) -> None:
        for old_owner in ("stale:1", "old:1.0"):
            with self.subTest(old_owner=old_owner):
                error = self.assert_main_refuses_without_mutation(TASK_TEXT, old_owner=old_owner)
                self.assertIn("does not equal --old-manager-target", error)

    def test_rejects_new_owner_equal_to_runat_or_its_alias(self) -> None:
        for new_owner in ("worker:7.0", "worker:7", "worker:07.00"):
            with self.subTest(new_owner=new_owner):
                error = self.assert_main_refuses_without_mutation(TASK_TEXT, new_owner=new_owner)
                self.assertIn("different from task `runat`", error)

    def test_rejects_missing_duplicate_and_invalid_frontmatter_owner(self) -> None:
        cases = {
            "no frontmatter": "body only\n",
            "unterminated frontmatter": TASK_TEXT.rsplit("---\r\n", 1)[0],
            "missing owner": TASK_TEXT.replace("managerat:\told:1 \t\r\n", ""),
            "duplicate owner": TASK_TEXT.replace("managerat:\told:1 \t\r\n", "managerat: old:1\r\nmanagerat: old:1\r\n"),
            "invalid owner": TASK_TEXT.replace("managerat:\told:1 \t", "managerat: invalid"),
            "invalid status": TASK_TEXT.replace("status: blocked", "status: waiting"),
        }
        for name, text in cases.items():
            with self.subTest(name=name):
                _ = self.assert_main_refuses_without_mutation(text)

    def test_rejects_new_owner_equal_to_runat_even_for_manager_task(self) -> None:
        manager_task = TASK_TEXT.replace("is_manager: false", "is_manager: true")
        error = self.assert_main_refuses_without_mutation(manager_task, new_owner="worker:7")
        self.assertIn("different from task `runat`", error)

    def test_rejects_task_path_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = parent / "root"
            root.mkdir()
            outside = parent / "outside.md"
            outside.write_bytes(TASK_TEXT.encode())
            argv = self.migration_argv(root)
            argv[argv.index("x.md")] = "../outside.md"
            with contextlib.redirect_stderr(io.StringIO()), patch("omo_manager.omo_task.tmux") as tmux:
                self.assertEqual(1, main(argv))
            self.assertEqual(TASK_TEXT.encode(), outside.read_bytes())
            self.assertFalse((root / "TODO.md").exists())
            tmux.assert_not_called()

    def test_rejects_launch_or_link_options_in_migration_mode(self) -> None:
        conflicts = (
            ["--tmux-session", "worker"],
            ["--workdir", "/tmp"],
            ["--manager-target", "manager:9"],
            ["--no-link"],
            ["--tool", "codex"],
            ["--is-manager"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            original = task.read_bytes()
            for conflict in conflicts:
                with self.subTest(conflict=conflict):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        _ = main([*self.migration_argv(root), *conflict])
                    self.assertEqual(original, task.read_bytes())
                    self.assertEqual(todo_bytes, todo.read_bytes())

    def test_concurrent_change_check_refuses_without_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            concurrent = task.read_bytes() + b"concurrent manager note\r\n"

            def transform_after_concurrent_write(text: str, old_owner: str, new_owner: str) -> str:
                updated = manager_owner_migration_text(text, old_owner, new_owner)
                task.write_bytes(concurrent)
                return updated

            with (
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                patch("omo_manager.omo_task.manager_owner_migration_text", side_effect=transform_after_concurrent_write),
                patch("omo_manager.omo_task.os.replace") as replace,
            ):
                self.assertEqual(1, main(self.migration_argv(root)))
            self.assertIn("changed while ownership migration was being prepared", stderr.getvalue())
            self.assertEqual(concurrent, task.read_bytes())
            self.assertEqual(todo_bytes, todo.read_bytes())
            self.assertEqual([], list(root.glob(".x.md.*")))
            replace.assert_not_called()

    def test_temp_write_failure_preserves_files_and_cleans_residue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            original = task.read_bytes()
            with (
                contextlib.redirect_stderr(io.StringIO()) as stderr,
                patch("omo_manager.omo_task.os.fsync", side_effect=OSError("forced fsync failure")),
                patch("omo_manager.omo_task.os.replace") as replace,
            ):
                self.assertEqual(1, main(self.migration_argv(root)))
            self.assertIn("forced fsync failure", stderr.getvalue())
            self.assertEqual(original, task.read_bytes())
            self.assertEqual(todo_bytes, todo.read_bytes())
            self.assertEqual([], list(root.glob(".x.md.*")))
            replace.assert_not_called()

    def test_two_migrations_serialize_and_one_refuses_stale_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, todo, todo_bytes = self.write_fixture(root)
            barrier = threading.Barrier(2)
            first_calls = 0
            first_calls_lock = threading.Lock()
            real_flock = fcntl.flock

            def synchronized_flock(fd: int, operation: int) -> None:
                nonlocal first_calls
                with first_calls_lock:
                    first_calls += 1
                    synchronize = first_calls <= 2
                if synchronize:
                    _ = barrier.wait(timeout=5)
                real_flock(fd, operation)

            def run_migration(new_owner: str) -> str:
                try:
                    migrate_manager_owner(task, "old:1", new_owner)
                except ValueError as exc:
                    return str(exc)
                return ""

            with contextlib.redirect_stdout(io.StringIO()), patch("omo_manager.omo_task.fcntl.flock", side_effect=synchronized_flock):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(run_migration, ("new:2", "other:3")))

            self.assertEqual(1, results.count(""))
            self.assertEqual(1, sum("does not equal --old-manager-target" in result for result in results))
            expected = {
                manager_owner_migration_text(TASK_TEXT, "old:1", "new:2").encode(),
                manager_owner_migration_text(TASK_TEXT, "old:1", "other:3").encode(),
            }
            self.assertIn(task.read_bytes(), expected)
            self.assertEqual(todo_bytes, todo.read_bytes())
            self.assertEqual([], list(root.glob(".x.md.*")))

    def test_help_shows_safe_operator_invocation_and_no_mutation_dry_run(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            _ = parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = stdout.getvalue()
        self.assertIn("--migrate-manager-owner --old-manager-target OLD", help_text)
        self.assertIn("--new-manager-target NEW [--dry-run]", help_text)
        self.assertIn("without changing files or tmux", help_text)


if __name__ == "__main__":
    _ = unittest.main()
