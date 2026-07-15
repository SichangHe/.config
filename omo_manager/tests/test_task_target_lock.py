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
