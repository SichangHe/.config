from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError
from omo_manager.omo_archived_custody import HUMAN_SOURCE, NEW_BLOCKER, OLD_BLOCKER, reconcile


class ArchivedScorerCustodyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "202608").mkdir()
        (self.root / "manager_mail").mkdir()
        self.files = {
            "old": self.root / "202608/dw_rescore_pages.md",
            "archive": self.root / "202608/old_todos.md",
            "todo": self.root / "TODO.md",
            "successor": self.root / "scorer_recovery_0926.md",
            "human": self.root / HUMAN_SOURCE,
        }
        self.files["old"].write_text(
            "---\nversion: v1.0.0\nstatus: blocked\n"
            f"blocked_on: {OLD_BLOCKER}\n"
            "runat: dw:39\ntool: codex\nmanagerat: wl:1\nis_manager: false\n"
            "pending_task_items: []\n---\nold scorer evidence\n",
            encoding="utf-8",
        )
        self.files["archive"].write_text(
            "previous:\ndw_rescore_pages.md dw:39\n"
            "202608/dw_rescore_pages.md dw:39\n", encoding="utf-8",
        )
        self.files["todo"].write_text("current:\nscorer_recovery_0926.md dw:2\n", encoding="utf-8")
        self.files["successor"].write_text(
            "---\nversion: v1.0.0\nstatus: running\nrunat: dw:2\ntool: codex\n"
            "managerat: wl:1\nis_manager: false\npending_task_items:\n"
            "  - preserve first open item\n  - preserve second open item\n---\nlive scorer evidence\n",
            encoding="utf-8",
        )
        self.files["human"].write_text(
            "Subject: Re: Acknowledged: DW repository cleanup and scorer recovery\n\n"
            "Become responsible for that\n\nold scoring task is archived and blocked\n",
            encoding="utf-8",
        )
        self.windows = patch(
            "omo_manager.omo_archived_custody.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "2\n64\n", ""),
        )
        self.window_mock = self.windows.start()
        self.addCleanup(self.windows.stop)
        authority_digest = hashlib.sha256(self.files["human"].read_bytes()).hexdigest()
        authority = patch("omo_manager.omo_archived_custody.HUMAN_SOURCE_SHA256", authority_digest)
        authority.start()
        self.addCleanup(authority.stop)

    def digests(self) -> dict[str, str]:
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in self.files.items()}

    def test_preflight_preserves_every_file_and_apply_changes_only_blocker(self) -> None:
        before = {name: path.read_bytes() for name, path in self.files.items()}
        expected = self.digests()
        digest = reconcile(self.root, expected)
        self.assertEqual(before, {name: path.read_bytes() for name, path in self.files.items()})
        self.assertEqual(digest, hashlib.sha256(before["old"].replace(OLD_BLOCKER.encode(), NEW_BLOCKER.encode())).hexdigest())
        self.assertEqual(digest, reconcile(self.root, expected, apply=True))
        self.assertEqual(self.files["old"].read_bytes(), before["old"].replace(OLD_BLOCKER.encode(), NEW_BLOCKER.encode()))
        for name in ("archive", "todo", "successor", "human"):
            self.assertEqual(before[name], self.files[name].read_bytes())
        with self.assertRaisesRegex(TaskFrontmatterError, "digest"):
            reconcile(self.root, expected, apply=True)

    def test_every_file_digest_is_required(self) -> None:
        expected = self.digests()
        for name in self.files:
            wrong = {**expected, name: "0" * 64}
            with self.subTest(name=name), self.assertRaisesRegex(TaskFrontmatterError, "digest|Human custody source"):
                reconcile(self.root, wrong, apply=True)

    def test_rejects_wrong_owner_status_queue_index_and_authority(self) -> None:
        changes = (
            ("old", "status: blocked", "status: done"),
            ("old", "runat: dw:39", "runat: dw:64"),
            ("old", "pending_task_items: []", "pending_task_items:\n  - waiting"),
            ("successor", "status: running", "status: blocked"),
            ("successor", "runat: dw:2", "runat: dw:3"),
            ("archive", "202608/dw_rescore_pages.md dw:39\n", ""),
            ("todo", "scorer_recovery_0926.md dw:2\n", ""),
            ("human", "Become responsible for that", "I have no opinion"),
        )
        for name, original, replacement in changes:
            with self.subTest(name=name, replacement=replacement):
                path = self.files[name]
                before = path.read_bytes()
                path.write_text(before.decode().replace(original, replacement), encoding="utf-8")
                try:
                    with self.assertRaises(TaskFrontmatterError):
                        reconcile(self.root, self.digests(), apply=True)
                    self.assertIn(OLD_BLOCKER, self.files["old"].read_text(encoding="utf-8"))
                finally:
                    path.write_bytes(before)

    def test_rejects_reappeared_historical_window_and_changed_inventory(self) -> None:
        expected = self.digests()
        for windows in ("2\n39\n64\n", "64\n", "2\n2\n64\n"):
            with self.subTest(windows=windows):
                self.window_mock.return_value = subprocess.CompletedProcess([], 0, windows, "")
                with self.assertRaisesRegex(TaskFrontmatterError, "window"):
                    reconcile(self.root, expected, apply=True)
        self.window_mock.side_effect = [
            subprocess.CompletedProcess([], 0, "2\n64\n", ""),
            subprocess.CompletedProcess([], 0, "2\n39\n64\n", ""),
        ]
        with self.assertRaisesRegex(TaskFrontmatterError, "window inventory changed"):
            reconcile(self.root, expected, apply=True)
        self.assertIn(OLD_BLOCKER, self.files["old"].read_text(encoding="utf-8"))

    def test_rejects_successor_in_previous_or_human_pending(self) -> None:
        todo = self.files["todo"]
        for heading in ("previous", "human pending"):
            with self.subTest(heading=heading):
                todo.write_text(f"{heading}:\nscorer_recovery_0926.md dw:2\n", encoding="utf-8")
                with self.assertRaisesRegex(TaskFrontmatterError, "current owner"):
                    reconcile(self.root, self.digests(), apply=True)
                self.assertIn(OLD_BLOCKER, self.files["old"].read_text(encoding="utf-8"))

    def test_rejects_second_current_target_owner(self) -> None:
        todo = self.files["todo"]
        todo.write_text("current:\nscorer_recovery_0926.md dw:2\nother_task.md dw:2\n", encoding="utf-8")
        with self.assertRaisesRegex(TaskFrontmatterError, "sole current"):
            reconcile(self.root, self.digests(), apply=True)
        self.assertIn(OLD_BLOCKER, self.files["old"].read_text(encoding="utf-8"))

    def test_failed_atomic_publish_preserves_all_bytes(self) -> None:
        before = {name: path.read_bytes() for name, path in self.files.items()}
        with patch("omo_manager.omo_archived_custody.replace_if_unchanged_locked", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                reconcile(self.root, self.digests(), apply=True)
        self.assertEqual(before, {name: path.read_bytes() for name, path in self.files.items()})


if __name__ == "__main__":
    unittest.main()
