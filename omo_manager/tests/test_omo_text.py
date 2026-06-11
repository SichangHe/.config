from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_text import main


class OmoTextTests(unittest.TestCase):
    def test_temp_creates_private_file_under_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_tempdir = tempfile.tempdir
            tempfile.tempdir = None
            out = io.StringIO()
            try:
                with patch.dict(os.environ, {"TMPDIR": tmp}), contextlib.redirect_stdout(out):
                    self.assertEqual(0, main(["temp", "--kind", "worker-prompt"]))
            finally:
                tempfile.tempdir = old_tempdir
            path = Path(out.getvalue().strip())
            self.assertEqual(Path(tmp), path.parent)
            self.assertTrue(path.is_file())
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_temp_rejects_unknown_kind(self) -> None:
        self.assertEqual(2, main(["temp", "--kind", "scratch"]))

    def test_email_uses_subject_and_body_files(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_text.subprocess.run", side_effect=fake_run):
            root = Path(tmp)
            subject = root / "subject.txt"
            body = root / "body.md"
            subject.write_text("- subject with $HOME and `date`\n", encoding="utf-8")
            body.write_text("body with $(date), `x`, > redirection, and 'quotes'\n", encoding="utf-8")
            self.assertEqual(0, main(["email", "--subject-file", str(subject), "--body-file", str(body)]))

        self.assertEqual("--subject-file", calls[0][1])
        self.assertEqual(str(subject), calls[0][2])
        self.assertEqual("--message-file", calls[0][3])
        self.assertEqual(str(body), calls[0][4])

    def test_email_delegates_subject_file_to_email_helper(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_text.subprocess.run", side_effect=fake_run):
            root = Path(tmp)
            subject = root / "subject.txt"
            body = root / "body.md"
            subject.write_text("subject\n", encoding="utf-8")
            body.write_text("body\n", encoding="utf-8")
            self.assertEqual(0, main(["email", "--subject-file", str(subject), "--body-file", str(body)]))

        self.assertIn("--subject-file", calls[0])
        self.assertEqual(str(subject), calls[0][calls[0].index("--subject-file") + 1])

    def test_rejects_multiline_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subject = root / "subject.txt"
            body = root / "body.md"
            subject.write_text("one\ntwo\n", encoding="utf-8")
            body.write_text("body\n", encoding="utf-8")
            self.assertEqual(1, main(["email", "--subject-file", str(subject), "--body-file", str(body)]))

    def test_rejects_stdin_subject_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("body\n", encoding="utf-8")
            self.assertEqual(2, main(["email", "--subject-file", "-", "--body-file", str(body)]))

    def test_tmux_body_file_is_preserved(self) -> None:
        calls: list[list[str]] = []
        seen_body = ""

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            nonlocal seen_body
            calls.append(command)
            seen_body = Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_text.subprocess.run", side_effect=fake_run):
            body = Path(tmp) / "prompt.md"
            body.write_text("- leading hyphen\n$HOME `date` $(date) > file\n", encoding="utf-8")
            self.assertEqual(0, main(["tmux", "--target", "cfg:1.0", "--body-file", str(body), "--enter"]))

        self.assertIn("--message-file", calls[0])
        self.assertEqual("- leading hyphen\n$HOME `date` $(date) > file\n", seen_body)

    def test_rejects_stdin_body_file(self) -> None:
        self.assertEqual(1, main(["tmux", "--target", "cfg:1.0", "--body-file", "-"]))

    def test_task_delegates_prompt_file(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as tmp, patch("omo_manager.omo_text.subprocess.run", side_effect=fake_run):
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("- prompt\n", encoding="utf-8")
            self.assertEqual(0, main(["task", "--task-file", "x.md", "--body-file", str(prompt), "--tmux-session", "cfg", "--workdir", tmp, "--reasoning-effort", "xhigh"]))

        self.assertIn("--prompt-file", calls[0])
        self.assertEqual(str(prompt), calls[0][calls[0].index("--prompt-file") + 1])
        self.assertIn("--reasoning-effort", calls[0])


if __name__ == "__main__":
    unittest.main()
