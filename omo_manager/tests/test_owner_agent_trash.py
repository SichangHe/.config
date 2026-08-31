from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from omo_manager.omo_manager_mail_compress import (
    GmailMetadata,
    MailRecord,
    cmd_owner_trash_agent_mail,
    is_agent_to_human_record,
)


class FakeClient:
    def __init__(self) -> None:
        self.uid_calls: list[tuple[str, ...]] = []
        self.selected = "INBOX"

    def uid(self, *args: str) -> tuple[str, list[bytes]]:
        self.uid_calls.append(args)
        if args == ("MOVE", "7,8", '"[Gmail]/Trash"'):
            return "OK", [b""]
        return "OK", [b""]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        del readonly
        self.selected = mailbox
        return "OK", [b""]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\HasNoChildren) "/" "[Gmail]/Trash"']

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", [b"IMAP4rev1 X-GM-EXT-1"]

    def logout(self) -> None:
        pass


class OwnerAgentTrashTests(unittest.TestCase):
    def test_boundary_includes_bcc_or_multiple_recipient_agent_mail(self) -> None:
        agent = MailRecord("7", "", "Agent <agent@example.test>", "Other <other@example.test>", "report", "sha")
        self.assertTrue(is_agent_to_human_record(agent, "agent@example.test", "human@example.test"))

    def test_boundary_excludes_human_mail(self) -> None:
        human = MailRecord("9", "", "Human <human@example.test>", "Agent <agent@example.test>", "reply", "sha")
        self.assertFalse(is_agent_to_human_record(human, "agent@example.test", "human@example.test"))

    def test_broad_move_preserves_read_state_and_prints_exact_counts_and_receipts(self) -> None:
        client = FakeClient()
        headers = [
            MailRecord("7", "", "Agent <agent@example.test>", "", "first", "sha"),
            MailRecord("8", "", "Agent <agent@example.test>", "Human <human@example.test>, Other <other@example.test>", "second", "sha"),
            MailRecord("9", "", "Human <human@example.test>", "Agent <agent@example.test>", "reply", "sha"),
        ]
        source_metadata = {
            "7": GmailMetadata("100", "200", "", r"\Inbox"),
            "8": GmailMetadata("101", "201", r"\Seen", r"\Inbox"),
        }
        trash_metadata = {
            "70": GmailMetadata("100", "200", "", r"\Trash"),
            "80": GmailMetadata("101", "201", r"\Seen", r"\Trash"),
        }
        output = io.StringIO()
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.gmail_extension_advertised", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.selected_inbox_counts", side_effect=[{"total": 87, "unread": 65}, {"total": 85, "unread": 64}]),
            patch("omo_manager.omo_manager_mail_compress.manager_candidate_uids", return_value=["7", "8", "9"]),
            patch("omo_manager.omo_manager_mail_compress.fetch_header_records", return_value=headers),
            patch("omo_manager.omo_manager_mail_compress.fetch_gmail_metadata_records_compatible", side_effect=[source_metadata, {"70": trash_metadata["70"]}, {"80": trash_metadata["80"]}]),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=[]),
            patch("omo_manager.omo_manager_mail_compress.gmail_message_uids", side_effect=[["70"], ["80"]]),
            redirect_stdout(output),
        ):
            self.assertEqual(0, cmd_owner_trash_agent_mail(argparse.Namespace()))
        receipt = json.loads(output.getvalue())
        self.assertEqual("omo-owner-agent-mail-trash/v1", receipt["schema"])
        self.assertEqual({"total": 87, "unread": 65}, receipt["before"])
        self.assertEqual({"total": 85, "unread": 64}, receipt["after"])
        self.assertEqual(2, receipt["moved_count"])
        self.assertEqual(
            [
                {"source_uid": "7", "gmail_msgid": "100", "trash_uid": "70", "unread": True},
                {"source_uid": "8", "gmail_msgid": "101", "trash_uid": "80", "unread": False},
            ],
            receipt["moved"],
        )
        self.assertEqual(0, receipt["permanent_deleted"])
        self.assertIn(("MOVE", "7,8", '"[Gmail]/Trash"'), client.uid_calls)
        self.assertFalse(any(call[0].upper() in {"STORE", "EXPUNGE", "DELETE"} for call in client.uid_calls))


if __name__ == "__main__":
    unittest.main()
