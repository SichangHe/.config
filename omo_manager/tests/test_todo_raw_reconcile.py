from __future__ import annotations

import hashlib
import errno
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_codex_stop import close_note
from omo_manager.omo_todo_raw_reconcile import Args
from omo_manager.omo_todo_raw_reconcile import PathSnapshot
from omo_manager.omo_todo_raw_reconcile import RAW_TODO_LINE
from omo_manager.omo_todo_raw_reconcile import ReconcileError
from omo_manager.omo_todo_raw_reconcile import parse_args
from omo_manager.omo_todo_raw_reconcile import reconcile
from omo_manager.omo_todo_raw_reconcile import recovery_scope_for
from omo_manager.omo_todo_raw_reconcile import rename_exchange
from omo_manager.omo_todo_raw_reconcile import updated_todo
import omo_manager.omo_todo_raw_reconcile as reconcile_module

SESSION_ID = "01a020c7-045d-7cb3-8365-563fcc214b09"


def task_text(*, status: str = "done", runat: str = "hcppb:1", pending: bool = False, closed: bool = True) -> str:
    queue = "pending_task_items:\n  - open" if pending else "pending_task_items: []"
    text = f"---\nversion: v1.0.0\nstatus: {status}\nrunat: {runat}\ntool: codex\nmanagerat: wl:30\nis_manager: false\n{queue}\n---\nterminal history\n"
    return text + (close_note(runat, SESSION_ID) if closed else "")


def todo_text(*, raw_line: str = RAW_TODO_LINE, task_row: str = "eda_reg_chat.md hcppb:1") -> str:
    return f"current:\nactive.md wl:2\n\nhuman pending:\nkeep before\n{raw_line}\nkeep after\n\nprevious:\nold.md wl:4\n{task_row}\nlow priority:\n{RAW_TODO_LINE} extra\n"


def args(root: Path, task: bytes, todo: bytes) -> Args:
    return Args(root, hashlib.sha256(todo).hexdigest(), hashlib.sha256(task).hexdigest(), SESSION_ID)


def recovery_entries(root: Path) -> list[Path]:
    scope = recovery_scope_for(root / "TODO.md")
    return list(scope.parent.glob(f"todo-raw-reconcile-recovery-{scope.name}-*"))


class TodoRawReconcileTests(unittest.TestCase):
    def make_root(self, root: Path, task: str, todo: str) -> tuple[Path, Path]:
        task_path = root / "eda_reg_chat.md"
        todo_path = root / "TODO.md"
        _ = task_path.write_text(task, encoding="utf-8")
        _ = todo_path.write_text(todo, encoding="utf-8")
        return task_path, todo_path

    def test_removes_only_exact_human_pending_line_and_preserves_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)

            reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            updated = todo_path.read_text(encoding="utf-8")
            self.assertNotIn(f"\n{RAW_TODO_LINE}\nkeep after", updated)
            self.assertIn(f"low priority:\n{RAW_TODO_LINE} extra\n", updated)
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), updated)
            recovery = recovery_entries(root)
            self.assertEqual(1, len(recovery))
            self.assertEqual(todo, recovery[0].read_text(encoding="utf-8"))
            self.assertFalse(recovery[0].is_relative_to(root))

    def test_success_exchange_uses_owner_only_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            _task_path, todo_path = self.make_root(root, task, todo)
            observed: list[tuple[Path, int]] = []

            def inspect(left: Path, right: Path, *_args: object, **_kwargs: object):
                observed.append((left.parent, stat.S_IMODE(left.parent.stat().st_mode)))
                return rename_exchange(left, right)

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=inspect):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(1, len(observed))
            self.assertNotEqual(root, observed[0][0])
            self.assertEqual(0o700, observed[0][1])
            self.assertNotIn(RAW_TODO_LINE + "\nkeep after", todo_path.read_text(encoding="utf-8"))
            self.assertFalse(observed[0][0].exists())
            recovery = recovery_entries(root)
            self.assertEqual(1, len(recovery))
            self.assertEqual(0o700, stat.S_IMODE(recovery[0].parent.stat().st_mode))
            self.assertEqual(todo, recovery[0].read_text(encoding="utf-8"))

    def test_recovery_publication_collision_never_replaces_existing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            _task_path, todo_path = self.make_root(root, task, todo)
            real_publish = reconcile_module._rename_noreplace  # pyright: ignore[reportPrivateUsage]
            calls = 0

            def collide_once(source: str, destination: str, *, source_fd: int, destination_fd: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    collision = Path(f"/proc/self/fd/{destination_fd}/{destination}")
                    _ = collision.write_text("preexisting recovery evidence\n", encoding="utf-8")
                real_publish(source, destination, source_fd=source_fd, destination_fd=destination_fd)

            with patch.object(reconcile_module, "_rename_noreplace", side_effect=collide_once):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(2, calls)
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
            retained = sorted(entry.read_text(encoding="utf-8") for entry in recovery_entries(root))
            self.assertEqual(sorted(("preexisting recovery evidence\n", todo)), retained)

    def test_recovery_publication_error_rolls_back_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            with patch.object(reconcile_module, "_rename_noreplace", side_effect=OSError(errno.EIO, "injected publication failure")), self.assertRaisesRegex(ReconcileError, "TODO restored"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            private_recovery = list(recovery_scope_for(todo_path).glob("todo-raw-reconcile-*/replacement"))
            self.assertEqual(1, len(private_recovery))
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), private_recovery[0].read_text(encoding="utf-8"))

    def test_publication_error_and_watch_completion_error_roll_back_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            real_finish = reconcile_module._MetadataWatch.finish  # pyright: ignore[reportPrivateUsage]
            calls = 0

            def fail_first_finish(watch: object) -> bool:
                nonlocal calls
                calls += 1
                changed = real_finish(watch)  # pyright: ignore[reportArgumentType]
                if calls == 1:
                    raise RuntimeError("injected ordinary watch completion failure")
                return changed

            with (
                patch.object(reconcile_module, "_rename_noreplace", side_effect=OSError(errno.EIO, "injected publication failure")),
                patch.object(
                    reconcile_module._MetadataWatch,  # pyright: ignore[reportPrivateUsage]
                    "finish",
                    autospec=True,
                    side_effect=fail_first_finish,
                ),
                self.assertRaisesRegex(ReconcileError, "TODO restored"),
            ):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(2, calls)
            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            private_recovery = list(recovery_scope_for(todo_path).glob("todo-raw-reconcile-*/replacement"))
            self.assertEqual(1, len(private_recovery))
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), private_recovery[0].read_text(encoding="utf-8"))

    def test_publication_error_with_same_mode_chmod_preserves_todo_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            original_mode = stat.S_IMODE(todo_path.stat().st_mode)

            def chmod_then_fail(_source: str, _destination: str, *, source_fd: int, destination_fd: int) -> None:
                _ = source_fd, destination_fd
                time.sleep(0.01)
                todo_path.chmod(original_mode)
                raise OSError(errno.EIO, "injected publication failure after same-mode chmod")

            with patch.object(reconcile_module, "_rename_noreplace", side_effect=chmod_then_fail), self.assertRaisesRegex(ReconcileError, "recovery evidence retained"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
            private_recovery = list(recovery_scope_for(todo_path).glob("todo-raw-reconcile-*/replacement"))
            self.assertEqual(1, len(private_recovery))
            self.assertEqual(todo, private_recovery[0].read_text(encoding="utf-8"))

    def test_publication_error_with_pre_snapshot_chmod_preserves_todo_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            original_mode = stat.S_IMODE(todo_path.stat().st_mode)
            calls = 0

            def exchange_then_chmod(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal calls
                calls += 1
                watch = rename_exchange(left, right)
                if calls == 1:
                    time.sleep(0.01)
                    right.chmod(original_mode)
                return watch

            with (
                patch.object(reconcile_module, "rename_exchange", side_effect=exchange_then_chmod),
                patch.object(reconcile_module, "_rename_noreplace", side_effect=OSError(errno.EIO, "injected publication failure")),
                self.assertRaisesRegex(ReconcileError, "recovery evidence retained"),
            ):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(1, calls)
            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
            private_recovery = list(recovery_scope_for(todo_path).glob("todo-raw-reconcile-*/replacement"))
            self.assertEqual(1, len(private_recovery))
            self.assertEqual(todo, private_recovery[0].read_text(encoding="utf-8"))

    def test_publication_error_reports_pinned_path_after_private_directory_rename(self) -> None:
        for renamed in ("leaf", "scope"):
            with self.subTest(renamed=renamed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)

                def rename_then_fail(_source: str, _destination: str, *, source_fd: int, destination_fd: int) -> None:
                    private = Path(os.readlink(f"/proc/self/fd/{source_fd}"))
                    if renamed == "leaf":
                        os.rename(private, private.with_name("renamed-private-leaf"))
                    else:
                        scope = private.parent
                        recovery_root = Path(os.readlink(f"/proc/self/fd/{destination_fd}"))
                        os.rename(scope, recovery_root / "renamed-recovery-scope")
                    raise OSError(errno.EIO, "injected publication failure after rename")

                with (
                    patch.object(reconcile_module, "_rename_noreplace", side_effect=rename_then_fail),
                    self.assertRaisesRegex(ReconcileError, "TODO restored and recovery evidence retained") as captured,
                ):
                    reconcile(args(root, task.encode(), todo.encode()))

                reported_path = Path(str(captured.exception).rsplit(" at ", 1)[1])
                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
                self.assertTrue(reported_path.is_file())
                self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), reported_path.read_text(encoding="utf-8"))

    def test_private_directory_substitution_does_not_touch_victim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            victim = root / "victim"
            victim.mkdir(mode=0o755)
            victim_mode = stat.S_IMODE(victim.stat().st_mode)
            real_open = os.open

            def substitute(path: os.PathLike[str] | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                path_string = os.fspath(path)
                if path_string.startswith("todo-raw-reconcile-") and dir_fd is not None:
                    os.rmdir(path_string, dir_fd=dir_fd)
                    os.symlink(victim, path_string, dir_fd=dir_fd)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("omo_manager.omo_todo_raw_reconcile.os.open", side_effect=substitute), self.assertRaises(OSError):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            self.assertEqual(victim_mode, stat.S_IMODE(victim.stat().st_mode))
            self.assertEqual([], list(victim.iterdir()))

    def test_recovery_scope_substitution_does_not_touch_victim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            victim = root / "scope-victim"
            victim.mkdir(mode=0o755)
            victim_mode = stat.S_IMODE(victim.stat().st_mode)
            scope_name = recovery_scope_for(todo_path).name
            real_open = os.open

            def substitute(path: os.PathLike[str] | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                path_string = os.fspath(path)
                if path_string == scope_name and dir_fd is not None:
                    os.rmdir(path_string, dir_fd=dir_fd)
                    os.symlink(victim, path_string, dir_fd=dir_fd)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("omo_manager.omo_todo_raw_reconcile.os.open", side_effect=substitute), self.assertRaises(OSError):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            self.assertEqual(victim_mode, stat.S_IMODE(victim.stat().st_mode))
            self.assertEqual([], list(victim.iterdir()))

    def test_rejects_recovery_storage_inside_work_log_or_on_another_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            supplied = args(root, task.encode(), todo.encode())
            inside = Args(root, supplied.expected_todo_sha256, supplied.expected_task_sha256, SESSION_ID, root / "private")
            with self.assertRaisesRegex(ReconcileError, "outside the work-log root"):
                reconcile(inside)
            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))

            other_device = Path("/dev/shm")
            if other_device.is_dir() and other_device.stat().st_dev != root.stat().st_dev:
                with tempfile.TemporaryDirectory(dir=other_device) as recovery_tmp:
                    cross_device = Args(root, supplied.expected_todo_sha256, supplied.expected_task_sha256, SESSION_ID, Path(recovery_tmp) / "private")
                    with self.assertRaisesRegex(ReconcileError, "unsafe private recovery directory"):
                        reconcile(cross_device)
                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))

    def test_rejects_stale_digests_without_changes(self) -> None:
        for field in ("todo", "task"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                supplied = args(root, task.encode(), todo.encode())
                supplied = Args(
                    root,
                    "0" * 64 if field == "todo" else supplied.expected_todo_sha256,
                    "0" * 64 if field == "task" else supplied.expected_task_sha256,
                    SESSION_ID,
                )

                with self.assertRaisesRegex(ReconcileError, rf"(?i){field} digest"):
                    reconcile(supplied)

                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))

    def test_rejects_incomplete_or_mismatched_terminal_evidence(self) -> None:
        cases = {
            "status": (task_text(status="running"), todo_text(), "done non-manager"),
            "target": (task_text(runat="cppb:1"), todo_text(), "done non-manager"),
            "queue": (task_text(pending=True), todo_text(), "empty queue"),
            "pending marker": (task_text() + "(pending)\n", todo_text(), "pending delivery marker"),
            "close": (task_text(closed=False), todo_text(), "close-session evidence"),
            "row target": (task_text(), todo_text(task_row="eda_reg_chat.md hcppb:2"), "under previous"),
            "annotated row": (task_text(), todo_text(task_row="eda_reg_chat.md hcppb:1 done"), "under previous"),
            "row section": (task_text(), todo_text(task_row="other.md hcppb:1").replace("active.md wl:2", "eda_reg_chat.md hcppb:1"), "under previous"),
        }
        for name, (task, todo, error) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task_path, todo_path = self.make_root(root, task, todo)
                with self.assertRaisesRegex(ReconcileError, error):
                    reconcile(args(root, task.encode(), todo.encode()))
                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))

    def test_rejects_missing_duplicate_or_noncanonical_raw_line(self) -> None:
        cases = {
            "missing": todo_text(raw_line=f" {RAW_TODO_LINE}"),
            "duplicate": todo_text(raw_line=f"{RAW_TODO_LINE}\n{RAW_TODO_LINE}"),
            "duplicate section": todo_text().replace("human pending:", "human pending:\nother\nhuman pending:", 1),
        }
        for name, todo in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                _task_path, todo_path = self.make_root(root, task, todo)
                with self.assertRaisesRegex(ReconcileError, "one canonical human-pending"):
                    reconcile(args(root, task.encode(), todo.encode()))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))

    def test_todo_row_proof_uses_only_digest_bound_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            live_todo = todo_text()
            supplied_todo = todo_text(task_row="xxx.md hcppb:1")
            _task_path, todo_path = self.make_root(root, task, live_todo)

            with self.assertRaisesRegex(ReconcileError, "eda_reg_chat.md row under previous"):
                _ = updated_todo(supplied_todo.encode())

            self.assertEqual(live_todo, todo_path.read_text(encoding="utf-8"))

    def test_rechecks_task_and_todo_before_replacement(self) -> None:
        for changed in ("task", "todo"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)

                def race(payload: bytes) -> bytes:
                    result = updated_todo(payload)
                    path = task_path if changed == "task" else todo_path
                    _ = path.write_bytes(path.read_bytes() + b"concurrent\n")
                    return result

                with patch("omo_manager.omo_todo_raw_reconcile.updated_todo", side_effect=race), self.assertRaisesRegex(ReconcileError, "changed"):
                    reconcile(args(root, task.encode(), todo.encode()))

                expected_task = task + ("concurrent\n" if changed == "task" else "")
                expected_todo = todo if changed == "task" else todo + "concurrent\n"
                self.assertEqual(expected_task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(expected_todo, todo_path.read_text(encoding="utf-8"))

    def test_atomic_exchange_preserves_change_in_last_compare_window(self) -> None:
        for changed in ("todo", "task"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                n_calls = 0

                def race(left: Path, right: Path, *_args: object, **_kwargs: object):
                    nonlocal n_calls
                    n_calls += 1
                    if n_calls == 1:
                        path = todo_path if changed == "todo" else task_path
                        _ = path.write_bytes(path.read_bytes() + b"concurrent\n")
                    return rename_exchange(left, right)

                with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=race), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                    reconcile(args(root, task.encode(), todo.encode()))

                expected_task = task + ("concurrent\n" if changed == "task" else "")
                expected_todo = todo if changed == "task" else todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1)
                self.assertEqual(expected_task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(expected_todo, todo_path.read_text(encoding="utf-8"))
                if changed == "todo":
                    self.assertEqual(todo + "concurrent\n", recovery_entries(root)[0].read_text(encoding="utf-8"))

    def test_substituted_todo_symlink_is_preserved_without_following_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            victim = root / "victim"
            _ = victim.write_text("untouched\n", encoding="utf-8")
            victim.chmod(0o640)
            victim_mode = stat.S_IMODE(victim.stat().st_mode)
            n_calls = 0

            def substitute(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal n_calls
                n_calls += 1
                if n_calls == 1:
                    left.unlink()
                    left.symlink_to(victim)
                return rename_exchange(left, right)

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=substitute), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertTrue(todo_path.is_symlink())
            self.assertEqual(victim, todo_path.resolve())
            self.assertEqual("untouched\n", todo_path.read_text(encoding="utf-8"))
            self.assertEqual("untouched\n", victim.read_text(encoding="utf-8"))
            self.assertEqual(victim_mode, stat.S_IMODE(victim.stat().st_mode))
            self.assertEqual(todo, recovery_entries(root)[0].read_text(encoding="utf-8"))

    def test_regular_temporary_substitution_is_retained_off_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            n_calls = 0

            def substitute(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal n_calls
                n_calls += 1
                if n_calls == 1:
                    replacement = root / "replacement"
                    _ = replacement.write_text("unrelated regular replacement\n", encoding="utf-8")
                    os.replace(replacement, left)
                return rename_exchange(left, right)

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=substitute), self.assertRaisesRegex(ReconcileError, "recovery path"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual("unrelated regular replacement\n", todo_path.read_text(encoding="utf-8"))
            recovery = recovery_entries(root)
            self.assertEqual(1, len(recovery))
            self.assertEqual(todo, recovery[0].read_text(encoding="utf-8"))
            self.assertFalse(recovery[0].is_relative_to(root))

    def test_permission_change_during_exchange_is_not_undone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            original_mode = stat.S_IMODE(todo_path.stat().st_mode)
            n_calls = 0

            def chmod_race(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal n_calls
                n_calls += 1
                if n_calls == 1:
                    right.chmod(0o600)
                return rename_exchange(left, right)

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=chmod_race), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
            recovery = recovery_entries(root)[0]
            self.assertEqual(todo, recovery.read_text(encoding="utf-8"))
            self.assertEqual(0o600, stat.S_IMODE(recovery.stat().st_mode))
            self.assertNotEqual(original_mode, stat.S_IMODE(recovery.stat().st_mode))

    def test_replacement_permission_change_at_syscall_boundary_is_retained_off_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            real_exchange = reconcile_module._atomic_exchange_syscall  # pyright: ignore[reportPrivateUsage]

            def chmod_replacement(left: Path, right: Path) -> None:
                left.chmod(0o600)
                real_exchange(left, right)

            with patch.object(reconcile_module, "_atomic_exchange_syscall", side_effect=chmod_replacement), self.assertRaisesRegex(ReconcileError, "recovery path"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text().replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
            recovery = recovery_entries(root)
            self.assertEqual(1, len(recovery))
            self.assertEqual(todo, recovery[0].read_text(encoding="utf-8"))

    def test_same_mode_chmod_at_syscall_boundary_is_not_silently_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            original_mode = stat.S_IMODE(todo_path.stat().st_mode)
            real_exchange = reconcile_module._atomic_exchange_syscall  # pyright: ignore[reportPrivateUsage]
            n_calls = 0

            def chmod_same_mode(left: Path, right: Path) -> None:
                nonlocal n_calls
                n_calls += 1
                if n_calls == 1:
                    right.chmod(original_mode)
                real_exchange(left, right)

            with patch.object(reconcile_module, "_atomic_exchange_syscall", side_effect=chmod_same_mode), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            self.assertEqual(original_mode, stat.S_IMODE(todo_path.stat().st_mode))

    def test_same_mode_chmod_after_exchange_before_validation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            original_mode = stat.S_IMODE(todo_path.stat().st_mode)
            real_exchange = reconcile_module.rename_exchange
            n_calls = 0

            def chmod_after_exchange(left: Path, right: Path, *expected: PathSnapshot | None, witness: Path | None = None):
                nonlocal n_calls
                n_calls += 1
                watch = real_exchange(left, right, *expected, witness=witness)
                if n_calls == 1:
                    right.chmod(original_mode)
                return watch

            with patch.object(reconcile_module, "rename_exchange", side_effect=chmod_after_exchange), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            self.assertEqual(original_mode, stat.S_IMODE(todo_path.stat().st_mode))

    def test_post_exchange_todo_changes_are_preserved(self) -> None:
        for change in ("in place", "atomic save"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)

                def race(left: Path, right: Path, *_args: object, **_kwargs: object):
                    watch = rename_exchange(left, right)
                    if change == "in place":
                        _ = right.write_bytes(right.read_bytes() + b"concurrent\n")
                    else:
                        saved = root / "saved"
                        _ = saved.write_text("concurrent atomic save\n", encoding="utf-8")
                        os.replace(saved, right)
                    return watch

                with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=race), self.assertRaisesRegex(ReconcileError, "concurrent TODO change preserved"):
                    reconcile(args(root, task.encode(), todo.encode()))

                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                expected = "concurrent atomic save\n" if change == "atomic save" else todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1) + "concurrent\n"
                self.assertEqual(expected, todo_path.read_text(encoding="utf-8"))

    def test_post_exchange_task_change_rolls_back_todo_and_preserves_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            n_calls = 0

            def race(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal n_calls
                n_calls += 1
                watch = rename_exchange(left, right)
                if n_calls == 1:
                    _ = task_path.write_bytes(task_path.read_bytes() + b"concurrent\n")
                return watch

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=race), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task + "concurrent\n", task_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
            recovery = recovery_entries(root)
            self.assertEqual(1, len(recovery))
            self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), recovery[0].read_text(encoding="utf-8"))

    def test_task_change_during_exchange_watch_completion_rolls_back_todo(self) -> None:
        for change in ("in place", "atomic save"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                real_finish = reconcile_module._MetadataWatch.finish  # pyright: ignore[reportPrivateUsage]
                n_calls = 0

                def finish_with_task_mutation(watch: object) -> bool:
                    nonlocal n_calls
                    n_calls += 1
                    changed = real_finish(watch)  # pyright: ignore[reportArgumentType]
                    if n_calls == 1:
                        if change == "in place":
                            _ = task_path.write_bytes(task_path.read_bytes() + b"final-window task change\n")
                        else:
                            saved = root / "saved-task"
                            _ = saved.write_text("final-window atomic task save\n", encoding="utf-8")
                            os.replace(saved, task_path)
                    return changed

                with (
                    patch.object(
                        reconcile_module._MetadataWatch,  # pyright: ignore[reportPrivateUsage]
                        "finish",
                        autospec=True,
                        side_effect=finish_with_task_mutation,
                    ),
                    self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"),
                ):
                    reconcile(args(root, task.encode(), todo.encode()))

                expected_task = task + "final-window task change\n" if change == "in place" else "final-window atomic task save\n"
                self.assertEqual(expected_task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))

    def test_exchange_watch_completion_error_rolls_back_todo(self) -> None:
        for error in (InterruptedError("injected interruption"), RuntimeError("injected ordinary failure")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                real_finish = reconcile_module._MetadataWatch.finish  # pyright: ignore[reportPrivateUsage]
                n_calls = 0

                def failed_finish(watch: object) -> bool:
                    nonlocal n_calls
                    n_calls += 1
                    changed = real_finish(watch)  # pyright: ignore[reportArgumentType]
                    if n_calls == 1:
                        raise error
                    return changed

                with (
                    patch.object(
                        reconcile_module._MetadataWatch,  # pyright: ignore[reportPrivateUsage]
                        "finish",
                        autospec=True,
                        side_effect=failed_finish,
                    ),
                    self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"),
                ):
                    reconcile(args(root, task.encode(), todo.encode()))

                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo, todo_path.read_text(encoding="utf-8"))
                recovery = recovery_entries(root)
                self.assertEqual(1, len(recovery))
                self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), recovery[0].read_text(encoding="utf-8"))

    def test_todo_change_during_exchange_watch_completion_is_preserved(self) -> None:
        for change in ("in place", "atomic save"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                real_finish = reconcile_module._MetadataWatch.finish  # pyright: ignore[reportPrivateUsage]
                n_calls = 0

                def finish_with_todo_mutation(watch: object) -> bool:
                    nonlocal n_calls
                    n_calls += 1
                    changed = real_finish(watch)  # pyright: ignore[reportArgumentType]
                    if n_calls == 1:
                        if change == "in place":
                            _ = todo_path.write_bytes(todo_path.read_bytes() + b"final-window TODO change\n")
                        else:
                            saved = root / "saved-todo"
                            _ = saved.write_text("final-window atomic TODO save\n", encoding="utf-8")
                            os.replace(saved, todo_path)
                    return changed

                with (
                    patch.object(
                        reconcile_module._MetadataWatch,  # pyright: ignore[reportPrivateUsage]
                        "finish",
                        autospec=True,
                        side_effect=finish_with_todo_mutation,
                    ),
                    self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"),
                ):
                    reconcile(args(root, task.encode(), todo.encode()))

                installed_todo = todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1)
                expected_todo = installed_todo + "final-window TODO change\n" if change == "in place" else "final-window atomic TODO save\n"
                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(expected_todo, todo_path.read_text(encoding="utf-8"))

    def test_nonregular_todo_change_after_exchange_is_not_displaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            victim = root / "concurrent-symlink-target"
            _ = victim.write_text("concurrent target bytes\n", encoding="utf-8")
            saved = root / "concurrent-symlink-save"
            saved.symlink_to(victim)
            real_finish = reconcile_module._MetadataWatch.finish  # pyright: ignore[reportPrivateUsage]
            calls = 0

            def install_symlink_after_finish(watch: object) -> bool:
                nonlocal calls
                calls += 1
                changed = real_finish(watch)  # pyright: ignore[reportArgumentType]
                if calls == 1:
                    os.replace(saved, todo_path)
                return changed

            with (
                patch.object(
                    reconcile_module._MetadataWatch,  # pyright: ignore[reportPrivateUsage]
                    "finish",
                    autospec=True,
                    side_effect=install_symlink_after_finish,
                ),
                self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"),
            ):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task, task_path.read_text(encoding="utf-8"))
            self.assertTrue(todo_path.is_symlink())
            self.assertEqual(victim, todo_path.resolve())
            self.assertEqual("concurrent target bytes\n", todo_path.read_text(encoding="utf-8"))
            self.assertEqual(todo, recovery_entries(root)[0].read_text(encoding="utf-8"))

    def test_moved_recovery_change_is_never_exchanged_back_into_todo(self) -> None:
        for change in ("in place", "atomic save"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                real_watch_changed = reconcile_module.watch_changed
                n_calls = 0

                def mutate_recovery(watch: object) -> bool:
                    nonlocal n_calls
                    changed = real_watch_changed(watch)  # pyright: ignore[reportArgumentType]
                    n_calls += 1
                    if n_calls == 1:
                        recovery = recovery_entries(root)[0]
                        if change == "in place":
                            _ = recovery.write_text("unrelated recovery mutation\n", encoding="utf-8")
                        else:
                            saved = root / "saved-recovery"
                            _ = saved.write_text("unrelated recovery save\n", encoding="utf-8")
                            os.replace(saved, recovery)
                    return changed

                with patch.object(reconcile_module, "watch_changed", side_effect=mutate_recovery), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                    reconcile(args(root, task.encode(), todo.encode()))

                expected_recovery = "unrelated recovery mutation\n" if change == "in place" else "unrelated recovery save\n"
                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
                self.assertEqual(expected_recovery, recovery_entries(root)[0].read_text(encoding="utf-8"))

    def test_moved_recovery_change_before_source_validation_is_never_restored(self) -> None:
        for change in ("in place", "atomic save"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = task_text()
                todo = todo_text()
                task_path, todo_path = self.make_root(root, task, todo)
                real_publish = reconcile_module._rename_noreplace  # pyright: ignore[reportPrivateUsage]

                def move_then_mutate(source: str, destination: str, *, source_fd: int, destination_fd: int) -> None:
                    real_publish(source, destination, source_fd=source_fd, destination_fd=destination_fd)
                    if source == "replacement" and destination.startswith("todo-raw-reconcile-recovery-"):
                        recovery = Path(f"/proc/self/fd/{destination_fd}/{destination}")
                        if change == "in place":
                            _ = recovery.write_text("early recovery corruption\n", encoding="utf-8")
                        else:
                            saved = root / "saved-early-recovery"
                            _ = saved.write_text("early recovery replacement\n", encoding="utf-8")
                            os.replace(saved, recovery)

                with patch.object(reconcile_module, "_rename_noreplace", side_effect=move_then_mutate), self.assertRaisesRegex(ReconcileError, "atomic reconciliation boundary"):
                    reconcile(args(root, task.encode(), todo.encode()))

                expected_recovery = "early recovery corruption\n" if change == "in place" else "early recovery replacement\n"
                self.assertEqual(task, task_path.read_text(encoding="utf-8"))
                self.assertEqual(todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1), todo_path.read_text(encoding="utf-8"))
                self.assertEqual(expected_recovery, recovery_entries(root)[0].read_text(encoding="utf-8"))

    def test_concurrent_atomic_save_during_rollback_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            n_calls = 0

            def race(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal n_calls
                n_calls += 1
                if n_calls == 2:
                    saved = root / "saved"
                    _ = saved.write_text("save during rollback\n", encoding="utf-8")
                    os.replace(saved, right)
                watch = rename_exchange(left, right)
                if n_calls == 1:
                    _ = task_path.write_bytes(task_path.read_bytes() + b"concurrent\n")
                return watch

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=race), self.assertRaisesRegex(ReconcileError, "preserved during atomic rollback"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task + "concurrent\n", task_path.read_text(encoding="utf-8"))
            self.assertEqual("save during rollback\n", todo_path.read_text(encoding="utf-8"))

    def test_atomic_save_in_rollback_syscall_window_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            real_syscall = reconcile_module._atomic_exchange_syscall  # pyright: ignore[reportPrivateUsage]
            real_exchange = rename_exchange
            syscall_calls = 0
            exchange_calls = 0

            def mutate_inside_second_syscall(left: Path, right: Path) -> None:
                nonlocal syscall_calls
                syscall_calls += 1
                if syscall_calls == 2:
                    saved = root / "rollback-window-save"
                    _ = saved.write_text("save inside rollback syscall window\n", encoding="utf-8")
                    os.replace(saved, right)
                real_syscall(left, right)

            def trigger_rollback(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal exchange_calls
                exchange_calls += 1
                watch = real_exchange(left, right)
                if exchange_calls == 1:
                    _ = task_path.write_bytes(task_path.read_bytes() + b"concurrent task change\n")
                return watch

            with (
                patch.object(reconcile_module, "_atomic_exchange_syscall", side_effect=mutate_inside_second_syscall),
                patch.object(reconcile_module, "rename_exchange", side_effect=trigger_rollback),
                self.assertRaisesRegex(ReconcileError, "preserved during atomic rollback"),
            ):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(3, syscall_calls)
            self.assertEqual(task + "concurrent task change\n", task_path.read_text(encoding="utf-8"))
            self.assertEqual("save inside rollback syscall window\n", todo_path.read_text(encoding="utf-8"))

    def test_rollback_time_temp_substitution_is_retained_off_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = task_text()
            todo = todo_text()
            task_path, todo_path = self.make_root(root, task, todo)
            n_calls = 0

            def race(left: Path, right: Path, *_args: object, **_kwargs: object):
                nonlocal n_calls
                n_calls += 1
                if n_calls == 2:
                    saved = root / "unrelated"
                    _ = saved.write_text("unrelated temp replacement\n", encoding="utf-8")
                    os.replace(saved, left)
                watch = rename_exchange(left, right)
                if n_calls == 1:
                    _ = task_path.write_bytes(task_path.read_bytes() + b"concurrent\n")
                return watch

            with patch("omo_manager.omo_todo_raw_reconcile.rename_exchange", side_effect=race), self.assertRaisesRegex(ReconcileError, "rollback-path substitution retained"):
                reconcile(args(root, task.encode(), todo.encode()))

            self.assertEqual(task + "concurrent\n", task_path.read_text(encoding="utf-8"))
            expected_todo = todo.replace(f"{RAW_TODO_LINE}\nkeep after", "keep after", 1)
            self.assertEqual(expected_todo, todo_path.read_text(encoding="utf-8"))
            recovery = recovery_entries(root)
            self.assertEqual(1, len(recovery))
            self.assertEqual("unrelated temp replacement\n", recovery[0].read_text(encoding="utf-8"))

    def test_parser_requires_all_hash_bound_evidence(self) -> None:
        parsed = parse_args(
            [
                "--root",
                "/tmp/work-logs",
                "--recovery-root",
                "/tmp/omo-manager-recovery",
                "--expected-todo-sha256",
                "a" * 64,
                "--expected-task-sha256",
                "b" * 64,
                "--close-session-id",
                SESSION_ID,
            ]
        )
        self.assertEqual(SESSION_ID, parsed.close_session_id)
        for flag, value in (("--expected-todo-sha256", "A" * 64), ("--expected-task-sha256", "short"), ("--close-session-id", "session")):
            argv = [
                "--root",
                "/tmp/work-logs",
                "--recovery-root",
                "/tmp/omo-manager-recovery",
                "--expected-todo-sha256",
                "a" * 64,
                "--expected-task-sha256",
                "b" * 64,
                "--close-session-id",
                SESSION_ID,
            ]
            argv[argv.index(flag) + 1] = value
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                _ = parse_args(argv)


if __name__ == "__main__":
    _ = unittest.main()
