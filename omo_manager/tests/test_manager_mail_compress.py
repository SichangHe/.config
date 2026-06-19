from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omo_manager.omo_manager_mail_compress import MailRecord, ensure_empty_private_dir, export_body, is_manager_record, parse_uid_text, write_private


class ManagerMailCompressTests(unittest.TestCase):
    def test_parse_uid_text_dedupes_and_accepts_commas_or_space(self) -> None:
        self.assertEqual(["5841", "5842", "5843"], parse_uid_text("5841,5842 5841\n5843"))

    def test_parse_uid_text_rejects_non_decimal_uid(self) -> None:
        with self.assertRaises(ValueError):
            parse_uid_text("5841:*")

    def test_manager_record_boundary_requires_self_and_manager_subject(self) -> None:
        self.assertTrue(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "", "Re: [a] x", "sha"),
                "me@example.test",
            )
        )
        self.assertTrue(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "", "Re: [omo_manager] x", "sha"),
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "Other <other@example.test>", "", "Re: [omo_manager] x", "sha"),
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "me@example.test via Other <other@example.test>", "", "Re: [omo_manager] x", "sha"),
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "", "[omo] x", "sha"),
                "me@example.test",
            )
        )

    def test_export_body_omits_sender_and_keeps_body(self) -> None:
        body = export_body(MailRecord("7", "date", "me@example.test", "me@example.test", "[omo_manager] topic", "abc", "private body\n"))
        self.assertIn("UID: 7\n", body)
        self.assertIn("Subject: [omo_manager] topic\n", body)
        self.assertIn("private body\n", body)
        self.assertNotIn("me@example.test", body)

    def test_write_private_creates_owner_only_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.txt"
            write_private(path, "x")
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual("x", path.read_text(encoding="utf-8"))

    def test_write_private_resets_existing_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.txt"
            path.write_text("old", encoding="utf-8")
            path.chmod(0o644)
            write_private(path, "new")
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual("new", path.read_text(encoding="utf-8"))

    def test_ensure_empty_private_dir_refuses_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export"
            path.mkdir()
            (path / "old.txt").write_text("old", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ensure_empty_private_dir(path)

    def test_ensure_empty_private_dir_creates_owner_only_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export"
            ensure_empty_private_dir(path)
            self.assertEqual(0o700, path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
