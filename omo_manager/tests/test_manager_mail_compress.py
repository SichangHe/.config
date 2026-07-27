from __future__ import annotations

import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_mail_compress import MailRecord, cmd_mark_seen, cmd_trash_superseded, ensure_empty_private_dir, export_body, imap_quoted, is_manager_record, mail_boundary, mailbox_exists, parse_uid_text, record_from_msg, write_private


class FakeClient:
    def __init__(self, uid_results: dict[tuple[str, ...], tuple[str, list[bytes | tuple[bytes, bytes]]] | list[tuple[str, list[bytes | tuple[bytes, bytes]]]]], mailboxes: list[bytes] | None = None) -> None:
        self.uid_results = uid_results
        self.mailboxes = mailboxes if mailboxes is not None else [b'(\\HasNoChildren) "/" "[Gmail]/Trash"']
        self.uid_calls: list[tuple[str, ...]] = []
        self.logged_out = False

    def uid(self, *args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
        self.uid_calls.append(args)
        result = self.uid_results.get(args, ("OK", [b""]))
        if isinstance(result, list):
            return result.pop(0) if result else ("OK", [b""])
        return result

    def list(self) -> tuple[str, list[bytes]]:
        return ("OK", self.mailboxes)

    def logout(self) -> None:
        self.logged_out = True


class Args:
    def __init__(self, uids: str = "", uid_file: Path | None = None, yes: bool = False) -> None:
        self.uids = uids
        self.uid_file = uid_file
        self.yes = yes


class ManagerMailCompressTests(unittest.TestCase):
    def test_parse_uid_text_dedupes_and_accepts_commas_or_space(self) -> None:
        self.assertEqual(["5841", "5842", "5843"], parse_uid_text("5841,5842 5841\n5843"))

    def test_parse_uid_text_rejects_non_decimal_uid(self) -> None:
        with self.assertRaises(ValueError):
            parse_uid_text("5841:*")

    def test_manager_record_boundary_requires_self_and_manager_subject(self) -> None:
        self.assertTrue(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "Human <me@example.test>", "Re: [a] x", "sha"),
                "me@example.test",
                "me@example.test",
            )
        )
        self.assertTrue(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "Human <me@example.test>", "Re: [omo_manager] x", "sha"),
                "me@example.test",
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "Other <other@example.test>", "Human <me@example.test>", "Re: [omo_manager] x", "sha"),
                "me@example.test",
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "me@example.test via Other <other@example.test>", "Human <me@example.test>", "Re: [omo_manager] x", "sha"),
                "me@example.test",
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "Other <other@example.test>", "Re: [omo_manager] x", "sha"),
                "me@example.test",
                "me@example.test",
            )
        )
        self.assertFalse(
            is_manager_record(
                MailRecord("1", "", "Human <me@example.test>", "Human <me@example.test>", "[omo] x", "sha"),
                "me@example.test",
                "me@example.test",
            )
        )

    def test_manager_record_boundary_accepts_agent_to_human_mail(self) -> None:
        self.assertTrue(
            is_manager_record(
                MailRecord("1", "", "Agent <agent@example.test>", "Human <human@example.test>", "[a] x", "sha"),
                "agent@example.test",
                "human@example.test",
            )
        )

    def test_manager_record_boundary_rejects_repeated_address_headers(self) -> None:
        msg = Message()
        msg["From"] = "Agent <agent@example.test>"
        msg["From"] = "Other <other@example.test>"
        msg["To"] = "Human <human@example.test>"
        msg["To"] = "Other <other@example.test>"
        msg["Subject"] = "[a] x"
        record = record_from_msg("1", msg)
        self.assertFalse(is_manager_record(record, "agent@example.test", "human@example.test"))

    def test_split_cleanup_rejects_wrong_human_mailbox(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()), self.assertRaisesRegex(RuntimeError, "does not match"):
            mail_boundary({"user": "other@example.test"})

    def test_imap_quoted_escapes_mailbox_name(self) -> None:
        self.assertEqual('"[Gmail]/Trash"', imap_quoted("[Gmail]/Trash"))
        self.assertEqual(r'"x\\y\"z"', imap_quoted(r'x\y"z'))

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

    def test_mailbox_exists_detects_gmail_trash(self) -> None:
        client = FakeClient({})
        self.assertTrue(mailbox_exists(client, "[Gmail]/Trash"))
        self.assertFalse(mailbox_exists(client, "Archive"))

    def test_trash_superseded_requires_yes(self) -> None:
        self.assertEqual(2, cmd_trash_superseded(Args(uids="7")))

    def test_mark_seen_is_retired_for_compression(self) -> None:
        self.assertEqual(2, cmd_mark_seen(Args(uids="7", yes=True)))

    def test_trash_superseded_moves_explicit_manager_uids(self) -> None:
        raw_msg = (
            b"From: Human <me@example.test>\r\n"
            b"To: Human <me@example.test>\r\n"
            b"Subject: [a] summary\r\n"
            b"Message-ID: <one@example.test>\r\n\r\n"
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7,8"): [("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"): ("OK", [(b"header", raw_msg)]),
                ("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""]),
            }
        )
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "me@example.test"})):
            self.assertEqual(0, cmd_trash_superseded(Args(uids="7,8", yes=True)))
        self.assertIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)
        self.assertTrue(client.logged_out)

    def test_trash_superseded_refuses_boundary_mismatch(self) -> None:
        raw_msg = (
            b"From: Other <other@example.test>\r\n"
            b"To: Human <me@example.test>\r\n"
            b"Subject: [a] summary\r\n"
            b"Message-ID: <one@example.test>\r\n\r\n"
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"): ("OK", [(b"header", raw_msg)]),
            }
        )
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "me@example.test"})):
            self.assertEqual(1, cmd_trash_superseded(Args(uids="7", yes=True)))
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)


if __name__ == "__main__":
    unittest.main()
