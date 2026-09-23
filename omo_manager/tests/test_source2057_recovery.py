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


class Source2057RecoveryTests(unittest.TestCase):
    def task_text(self) -> str:
        return f"""---
version: v1.0.0
status: long_running
blocked_on: human
runat: DeGenTWeb_writeup:0
tool: codex
managerat: wl:1
is_manager: false
pending_task_items:
  - '{omo_pending.SOURCE2057_PRESERVED_ITEM}'
---
(record and delegate manager_mail/85c5dff58359-2057.txt)
"""

    @staticmethod
    def sent_message(body: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = "agent@example.test"
        message["To"] = "human@example.test"
        message["Subject"] = omo_pending.SOURCE2057_SUBJECT
        message["Message-ID"] = omo_pending.SOURCE2057_MESSAGE_ID
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

    def test_recovery_records_exact_answer_and_preserves_paper_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2057_TASK
            source = root / omo_pending.SOURCE2057_SOURCE
            source.parent.mkdir()
            source.write_text("authoritative Source-2057\n", encoding="utf-8")
            original = self.task_text()
            task.write_text(original, encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2057_ROOT", str(root)),
                patch.object(omo_pending, "SOURCE2057_TASK_SHA256", hashlib.sha256(original.encode()).hexdigest()),
                patch.object(omo_pending, "SOURCE2057_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [
                    [
                        omo_pending.SOURCE2057_AUTHORIZATION,
                        omo_pending.SOURCE2057_OWNER,
                        omo_pending.SOURCE2057_TASK,
                        omo_pending.SOURCE2057_MANAGER,
                        omo_pending.SOURCE2057_CLAIM_TASK_SHA256,
                        omo_pending.SOURCE2057_CLAIM_NOTICE_KEY,
                        omo_pending.SOURCE2057_CLAIM_SEMANTIC_KEY,
                    ]
                ], "")),
                patch.object(omo_pending, "ordinary_completion_participant_evidence", return_value=("a" * 64, "b" * 64)) as verify,
            ):
                self.assertEqual(0, omo_pending.recover_source2057(root, task))
                record = omo_pending.source2057_record_comment()

            verify.assert_called_once_with(
                omo_pending.SOURCE2057_MESSAGE_ID,
                omo_pending.SOURCE2057_SUBJECT_SHA256,
                omo_pending.SOURCE2057_BODY_SHA256,
            )
            updated = task.read_text(encoding="utf-8")
            metadata = omo_pending.read_task_metadata(task, root)
            self.assertEqual((omo_pending.SOURCE2057_PRESERVED_ITEM,), metadata.pending_task_items)
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("human", metadata.blocked_on)
            self.assertEqual(1, updated.count(record))
            self.assertIn(omo_pending.SOURCE2057_SUBJECT, record)
            self.assertIn(omo_pending.SOURCE2057_ITEM, record)
            self.assertIn(omo_pending.SOURCE2057_EVIDENCE, record)
            self.assertIn(hashlib.sha256(original.encode()).hexdigest(), record)

    def test_recovery_rejects_task_or_source_drift_before_sent_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2057_TASK
            source = root / omo_pending.SOURCE2057_SOURCE
            source.parent.mkdir()
            source.write_text("changed authority\n", encoding="utf-8")
            original = self.task_text()
            task.write_text(original, encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2057_ROOT", str(root)),
                patch.object(omo_pending, "SOURCE2057_TASK_SHA256", hashlib.sha256(original.encode()).hexdigest()),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as verify,
                self.assertRaisesRegex(BlockingError, "authority changed"),
            ):
                omo_pending.recover_source2057(root, task)
            verify.assert_not_called()

    def test_recovery_accepts_exact_sent_shape_and_rejects_quoted_old_text(self) -> None:
        settings = type(
            "Settings",
            (),
            {"agent_address": "agent@example.test", "human_address": "human@example.test", "app_password": "secret"},
        )()
        exact = self.sent_message("Exact Source-2057 answer")
        decoded = BytesParser(policy=policy.default).parsebytes(exact.as_bytes())
        exact_body = omo_completion_email.ordinary_sent_text(decoded)
        exact_body_sha256 = hashlib.sha256(exact_body.encode()).hexdigest()
        subject_sha256 = hashlib.sha256(omo_pending.SOURCE2057_SUBJECT.encode()).hexdigest()
        with (
            patch.dict("os.environ", {"OMO_COMPLETION_SENT_VERIFY_TIMEOUT_S": "0"}),
            patch.object(omo_completion_email, "configured_agent_mail", return_value=settings),
            patch.object(omo_completion_email.imaplib, "IMAP4_SSL", return_value=self.fake_imap(exact)),
        ):
            self.assertIsNotNone(
                omo_pending.ordinary_completion_participant_evidence(
                    omo_pending.SOURCE2057_MESSAGE_ID,
                    subject_sha256,
                    exact_body_sha256,
                )
            )
        quoted = self.sent_message(f"Quoted old text:\n> {exact_body}")
        with (
            patch.dict("os.environ", {"OMO_COMPLETION_SENT_VERIFY_TIMEOUT_S": "0"}),
            patch.object(omo_completion_email, "configured_agent_mail", return_value=settings),
            patch.object(omo_completion_email.imaplib, "IMAP4_SSL", return_value=self.fake_imap(quoted)),
        ):
            self.assertIsNone(
                omo_pending.ordinary_completion_participant_evidence(
                    omo_pending.SOURCE2057_MESSAGE_ID,
                    subject_sha256,
                    exact_body_sha256,
                )
            )

    def test_recovery_rejects_missing_authorization_before_sent_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2057_TASK
            source = root / omo_pending.SOURCE2057_SOURCE
            source.parent.mkdir()
            source.write_text("authoritative Source-2057\n", encoding="utf-8")
            original = self.task_text()
            task.write_text(original, encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2057_ROOT", str(root)),
                patch.object(omo_pending, "SOURCE2057_TASK_SHA256", hashlib.sha256(original.encode()).hexdigest()),
                patch.object(omo_pending, "SOURCE2057_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [], "")),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as verify,
                self.assertRaisesRegex(BlockingError, "authorization claim"),
            ):
                omo_pending.recover_source2057(root, task)
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
