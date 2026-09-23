from __future__ import annotations

import hashlib
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_pending
from omo_manager import omo_completion_email
from omo_manager.omo_blocking import BlockingError


class Source2059RecoveryTests(unittest.TestCase):
    @staticmethod
    def sent_message(body: str) -> EmailMessage:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "agent@example.test"
        message["To"] = "human@example.test"
        message["Subject"] = omo_pending.SOURCE2059_SUBJECT
        message["Message-ID"] = omo_pending.SOURCE2059_MESSAGE_ID
        message.set_content(body)
        return message

    @staticmethod
    def fake_imap(message: EmailMessage) -> object:
        class FakeImap:
            def login(self, _address: str, _password: str) -> None:
                return None

            def select(self, _mailbox: str, *, readonly: bool) -> tuple[str, list[bytes]]:
                return "OK", []

            def uid(self, command: str, *_args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                return ("OK", [b"1"]) if command == "search" else ("OK", [(b"1", message.as_bytes())])

            def logout(self) -> None:
                return None

        return FakeImap()

    def task_text(self, *, owner: str = "DeGenTWeb_writeup:0", extra_item: str = "") -> str:
        items = [omo_pending.SOURCE2057_PRESERVED_ITEM]
        if extra_item:
            items.append(extra_item)
        queue = "\n".join(f"  - '{item}'" for item in items)
        return f"""---
version: v1.0.0
status: running
runat: {owner}
tool: codex
managerat: wl:1
is_manager: false
pending_task_items:
{queue}
session_id: {omo_pending.SOURCE2048_PAPER_SESSION}
---
(record and delegate manager_mail/85c5dff58359-2059.txt)
"""

    def recover(self, root: Path, task: Path) -> int:
        claim = [self.claim()]
        with (
            patch.object(omo_pending, "SOURCE2059_ROOT", str(root.resolve())),
            patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), claim, "")),
            patch.object(omo_pending, "ordinary_completion_participant_evidence", return_value=("a" * 64, "b" * 64)) as sent,
        ):
            result = omo_pending.recover_source2059(root, task)
        sent.assert_called_once_with(
            omo_pending.SOURCE2059_MESSAGE_ID,
            omo_pending.SOURCE2059_SUBJECT_SHA256,
            omo_pending.SOURCE2059_BODY_SHA256,
        )
        return result

    @staticmethod
    def claim() -> list[str]:
        return [
            omo_pending.SOURCE2059_AUTHORIZATION,
            omo_pending.SOURCE2059_OWNER,
            omo_pending.SOURCE2059_TASK,
            omo_pending.SOURCE2059_MANAGER,
            omo_pending.SOURCE2059_CLAIM_TASK_SHA256,
            omo_pending.SOURCE2059_CLAIM_NOTICE_KEY,
            omo_pending.SOURCE2059_CLAIM_SEMANTIC_KEY,
        ]

    def test_recovery_records_exact_item_and_preserves_owner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2059_TASK
            source = root / omo_pending.SOURCE2059_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2059\n", encoding="utf-8")
            original = self.task_text(extra_item="Existing delegated lookup")
            task.write_text(original, encoding="utf-8")
            with patch.object(omo_pending, "SOURCE2059_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()):
                self.assertEqual(0, self.recover(root, task))

            metadata = omo_pending.read_task_metadata(task, root)
            self.assertEqual(
                (omo_pending.SOURCE2057_PRESERVED_ITEM, "Existing delegated lookup", omo_pending.SOURCE2059_ITEM),
                metadata.pending_task_items,
            )
            self.assertEqual("running", metadata.status)
            self.assertEqual(omo_pending.SOURCE2059_OWNER, metadata.runat)
            self.assertEqual(omo_pending.SOURCE2048_PAPER_SESSION, metadata.session_id)
            updated = task.read_text(encoding="utf-8")
            self.assertEqual(1, updated.count("Source-2059 recovery:"))
            self.assertIn(omo_pending.SOURCE2059_MESSAGE_ID, updated)
            self.assertIn(omo_pending.SOURCE2059_AUTHORIZATION, updated)
            without_record = "".join(line for line in updated.splitlines(keepends=True) if not line.startswith("(Source-2059 recovery:"))
            canonical_before, removed = omo_pending.remove_pending_items(without_record, (omo_pending.SOURCE2059_ITEM,))
            self.assertEqual(1, removed)
            self.assertIn(hashlib.sha256(canonical_before.encode()).hexdigest(), updated)
            self.assertIn(hashlib.sha256(without_record.encode()).hexdigest(), updated)

            before_retry = task.read_bytes()
            with (
                patch.object(omo_pending, "SOURCE2059_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2059_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [self.claim()], "")),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
            ):
                self.assertEqual(0, omo_pending.recover_source2059(root, task))
            sent.assert_not_called()
            self.assertEqual(before_retry, task.read_bytes())

    def test_recovery_rejects_ambiguous_claim_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2059_TASK
            source = root / omo_pending.SOURCE2059_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2059\n", encoding="utf-8")
            task.write_text(self.task_text(), encoding="utf-8")
            duplicate = ["f" * 64, *self.claim()[1:]]
            with (
                patch.object(omo_pending, "SOURCE2059_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2059_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [self.claim(), duplicate], "")),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                self.assertRaisesRegex(BlockingError, "authorization claim"),
            ):
                omo_pending.recover_source2059(root, task)
            sent.assert_not_called()

    def test_recovery_rejects_malformed_duplicate_or_tampered_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2059_TASK
            source = root / omo_pending.SOURCE2059_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2059\n", encoding="utf-8")
            original = self.task_text()
            with patch.object(omo_pending, "SOURCE2059_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()):
                task.write_text(original, encoding="utf-8")
                self.assertEqual(0, self.recover(root, task))
                valid = task.read_text(encoding="utf-8")

                cases = (
                    valid.replace("Source-2059 recovery:", "Source-2059 recovery: malformed ", 1),
                    valid + next(line for line in valid.splitlines(keepends=True) if line.startswith("(Source-2059 recovery:")),
                    valid.replace('"canonical_before_task_sha256":"', '"canonical_before_task_sha256":"f', 1),
                )
                for changed in cases:
                    task.write_text(changed, encoding="utf-8")
                    before = task.read_bytes()
                    with (
                        patch.object(omo_pending, "SOURCE2059_ROOT", str(root.resolve())),
                        patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [self.claim()], "")),
                        patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                        self.assertRaisesRegex(BlockingError, "authority changed"),
                    ):
                        omo_pending.recover_source2059(root, task)
                    sent.assert_not_called()
                    self.assertEqual(before, task.read_bytes())

    def test_recovery_rejects_wrong_owner_before_sent_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2059_TASK
            source = root / omo_pending.SOURCE2059_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2059\n", encoding="utf-8")
            task.write_text(self.task_text(owner="other:0"), encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2059_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2059_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                self.assertRaisesRegex(BlockingError, "authority changed"),
            ):
                omo_pending.recover_source2059(root, task)
            sent.assert_not_called()

    def test_public_command_uses_authenticated_current_owner(self) -> None:
        args = omo_pending.parse_args(["recover-source2059"])
        self.assertEqual((omo_pending.SOURCE2059_ITEM,), args.items)
        with (
            patch.object(omo_pending, "current_pending_task", side_effect=BlockingError("ambiguous owner")),
            self.assertRaisesRegex(BlockingError, "ambiguous owner"),
        ):
            omo_pending.run(args, root=Path("/tmp/not-used"))

    def test_exact_acceptance_is_bound_and_other_body_is_rejected(self) -> None:
        settings = type(
            "Settings",
            (),
            {"agent_address": "agent@example.test", "human_address": "human@example.test", "app_password": "secret"},
        )()
        exact = self.sent_message(
            "I’ll fill the comment placeholders in the September 14–20 memo using the existing results and a quoted example, then send it back for your review."
        )
        body = omo_completion_email.ordinary_sent_text(BytesParser(policy=policy.default).parsebytes(exact.as_bytes()))
        self.assertEqual(omo_pending.SOURCE2059_BODY_SHA256, hashlib.sha256(body.encode()).hexdigest())
        with (
            patch.dict("os.environ", {"OMO_COMPLETION_SENT_VERIFY_TIMEOUT_S": "0"}),
            patch.object(omo_completion_email, "configured_agent_mail", return_value=settings),
            patch.object(omo_completion_email.imaplib, "IMAP4_SSL", return_value=self.fake_imap(exact)),
        ):
            self.assertIsNotNone(
                omo_pending.ordinary_completion_participant_evidence(
                    omo_pending.SOURCE2059_MESSAGE_ID,
                    omo_pending.SOURCE2059_SUBJECT_SHA256,
                    omo_pending.SOURCE2059_BODY_SHA256,
                )
            )
        changed = self.sent_message("Different acceptance")
        with (
            patch.dict("os.environ", {"OMO_COMPLETION_SENT_VERIFY_TIMEOUT_S": "0"}),
            patch.object(omo_completion_email, "configured_agent_mail", return_value=settings),
            patch.object(omo_completion_email.imaplib, "IMAP4_SSL", return_value=self.fake_imap(changed)),
        ):
            self.assertIsNone(
                omo_pending.ordinary_completion_participant_evidence(
                    omo_pending.SOURCE2059_MESSAGE_ID,
                    omo_pending.SOURCE2059_SUBJECT_SHA256,
                    omo_pending.SOURCE2059_BODY_SHA256,
                )
            )


if __name__ == "__main__":
    unittest.main()
