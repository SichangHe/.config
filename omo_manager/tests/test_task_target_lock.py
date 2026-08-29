from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task_lock import task_target_lock, task_target_lock_path


class TaskTargetLockTests(unittest.TestCase):
    def test_lock_path_is_independent_of_process_temp_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"TMPDIR": "/tmp/first"}):
                first = task_target_lock_path(root, "wl:3")
            with patch.dict(os.environ, {"TMPDIR": "/tmp/second"}):
                second = task_target_lock_path(root, "wl:3.0")

            self.assertEqual(first, second)

    def test_numeric_target_aliases_share_one_lock_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = task_target_lock_path(root, "guest_hees:5")

            self.assertEqual(expected, task_target_lock_path(root, "guest_hees:05"))
            self.assertEqual(expected, task_target_lock_path(root, "guest_hees:005.00"))
            self.assertEqual(task_target_lock_path(root, "guest_hees:5.2"), task_target_lock_path(root, "guest_hees:05.02"))

    def test_target_lock_rejects_hostile_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "locks" / "target"
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            outside = root / "outside"
            outside.write_text("foreign", encoding="utf-8")
            lock_path.symlink_to(outside)

            with patch("omo_manager.omo_task_lock.task_target_lock_path", return_value=lock_path), self.assertRaises(OSError), task_target_lock(root, "guest_hees:5"):
                self.fail("hostile target lock was acquired")

    def test_target_lock_rejects_hostile_directory_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "locks" / "target"
            lock_path.parent.mkdir(mode=0o777, parents=True, exist_ok=True)
            lock_path.parent.chmod(0o777)

            with patch("omo_manager.omo_task_lock.task_target_lock_path", return_value=lock_path), self.assertRaisesRegex(OSError, "lock directory is unsafe"), task_target_lock(root, "guest_hees:5"):
                self.fail("hostile target lock directory was accepted")

    def test_target_lock_rejects_rebound_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "locks" / "target"
            lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path.parent.chmod(0o700)
            real_open = os.open
            rebound = False

            def rebind_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                nonlocal rebound
                fd = real_open(path, flags, mode, dir_fd=dir_fd)
                if dir_fd is not None and os.fspath(path) == lock_path.name and not rebound:
                    rebound = True
                    os.unlink(lock_path.name, dir_fd=dir_fd)
                    replacement_fd = real_open(lock_path.name, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=dir_fd)
                    os.close(replacement_fd)
                return fd

            with patch("omo_manager.omo_task_lock.task_target_lock_path", return_value=lock_path), patch("omo_manager.omo_task_lock.os.open", side_effect=rebind_open), self.assertRaisesRegex(OSError, "entry changed"), task_target_lock(root, "guest_hees:5"):
                self.fail("rebound target lock entry was accepted")

    def test_zero_pane_aliases_serialize_ownership_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_acquired = threading.Event()
            release_first = threading.Event()
            second_acquired = threading.Event()

            def first() -> None:
                with task_target_lock(root, "wl:3"):
                    first_acquired.set()
                    self.assertTrue(release_first.wait(2))

            def second() -> None:
                self.assertTrue(first_acquired.wait(2))
                with task_target_lock(root, "wl:3.0"):
                    second_acquired.set()

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            second_thread.start()
            self.assertTrue(first_acquired.wait(2))
            self.assertFalse(second_acquired.wait(0.05))
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertTrue(second_acquired.is_set())
