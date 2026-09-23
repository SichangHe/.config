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


class Source2062RecoveryTests(unittest.TestCase):
    @staticmethod
    def sent_message(body: str) -> EmailMessage:
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = "agent@example.test"
        message["To"] = "human@example.test"
        message["Subject"] = omo_pending.SOURCE2062_SUBJECT
        message["Message-ID"] = omo_pending.SOURCE2062_MESSAGE_ID
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

    def task_text(
        self,
        *,
        owner: str = "DeGenTWeb_writeup:0",
        source2059: bool = True,
        source2059_record: bool = True,
    ) -> str:
        items = [omo_pending.SOURCE2057_PRESERVED_ITEM]
        if source2059:
            items.append(omo_pending.SOURCE2059_ITEM)
        queue = "\n".join(f"  - '{item}'" for item in items)
        text = f"""---
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
(record and delegate manager_mail/85c5dff58359-2062.txt)
"""
        if not source2059 or not source2059_record:
            return text
        canonical_before, removed = omo_pending.remove_pending_items(text, (omo_pending.SOURCE2059_ITEM,))
        self.assertEqual(1, removed)
        return omo_pending.append_comment(
            text,
            omo_pending.source2059_record_comment(
                hashlib.sha256(canonical_before.encode()).hexdigest(),
                hashlib.sha256(text.encode()).hexdigest(),
            ),
        )

    def recover(self, root: Path, task: Path) -> int:
        claim = [self.claim()]
        with (
            patch.object(omo_pending, "SOURCE2062_ROOT", str(root.resolve())),
            patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), claim, "")),
            patch.object(omo_pending, "ordinary_completion_participant_evidence", return_value=("a" * 64, "b" * 64)) as sent,
        ):
            result = omo_pending.recover_source2062(root, task)
        sent.assert_called_once_with(
            omo_pending.SOURCE2062_MESSAGE_ID,
            omo_pending.SOURCE2062_SUBJECT_SHA256,
            omo_pending.SOURCE2062_BODY_SHA256,
        )
        return result

    @staticmethod
    def claim() -> list[str]:
        return [
            omo_pending.SOURCE2062_AUTHORIZATION,
            omo_pending.SOURCE2062_OWNER,
            omo_pending.SOURCE2062_TASK,
            omo_pending.SOURCE2062_MANAGER,
            omo_pending.SOURCE2062_CLAIM_TASK_SHA256,
            omo_pending.SOURCE2062_CLAIM_NOTICE_KEY,
            omo_pending.SOURCE2062_CLAIM_SEMANTIC_KEY,
        ]

    def test_recovery_records_exact_item_and_preserves_source2059_and_owner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2062_TASK
            source = root / omo_pending.SOURCE2062_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2062\n", encoding="utf-8")
            original = self.task_text()
            task.write_text(original, encoding="utf-8")
            with patch.object(omo_pending, "SOURCE2062_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()):
                self.assertEqual(0, self.recover(root, task))
                recorded_task, added = omo_pending.add_pending_items(original, (omo_pending.SOURCE2062_ITEM,))
                self.assertEqual(1, added)
                record = omo_pending.source2062_record_comment(
                    hashlib.sha256(original.encode()).hexdigest(),
                    hashlib.sha256(recorded_task.encode()).hexdigest(),
                )

            metadata = omo_pending.read_task_metadata(task, root)
            self.assertEqual(
                (omo_pending.SOURCE2057_PRESERVED_ITEM, omo_pending.SOURCE2059_ITEM, omo_pending.SOURCE2062_ITEM),
                metadata.pending_task_items,
            )
            self.assertEqual("running", metadata.status)
            self.assertEqual(omo_pending.SOURCE2062_OWNER, metadata.runat)
            self.assertEqual(omo_pending.SOURCE2048_PAPER_SESSION, metadata.session_id)
            self.assertEqual(1, task.read_text(encoding="utf-8").count(record))

            before_retry = task.read_bytes()
            with (
                patch.object(omo_pending, "SOURCE2062_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2062_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [self.claim()], "")),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
            ):
                self.assertEqual(0, omo_pending.recover_source2062(root, task))
            sent.assert_not_called()
            self.assertEqual(before_retry, task.read_bytes())

    def test_recovery_rejects_ambiguous_claim_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2062_TASK
            source = root / omo_pending.SOURCE2062_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2062\n", encoding="utf-8")
            task.write_text(self.task_text(), encoding="utf-8")
            duplicate = ["f" * 64, *self.claim()[1:]]
            with (
                patch.object(omo_pending, "SOURCE2062_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2062_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "claims_rows", return_value=(Path("claims"), [self.claim(), duplicate], "")),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                self.assertRaisesRegex(BlockingError, "authorization claim"),
            ):
                omo_pending.recover_source2062(root, task)
            sent.assert_not_called()

    def test_recovery_rejects_wrong_owner_before_sent_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2062_TASK
            source = root / omo_pending.SOURCE2062_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2062\n", encoding="utf-8")
            task.write_text(self.task_text(owner="other:0"), encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2062_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2062_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                self.assertRaisesRegex(BlockingError, "authority changed"),
            ):
                omo_pending.recover_source2062(root, task)
            sent.assert_not_called()

    def test_recovery_rejects_missing_source2059_before_sent_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2062_TASK
            source = root / omo_pending.SOURCE2062_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2062\n", encoding="utf-8")
            task.write_text(self.task_text(source2059=False), encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2062_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2062_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                self.assertRaisesRegex(BlockingError, "authority changed"),
            ):
                omo_pending.recover_source2062(root, task)
            sent.assert_not_called()

    def test_recovery_rejects_source2059_item_without_durable_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / omo_pending.SOURCE2062_TASK
            source = root / omo_pending.SOURCE2062_SOURCE
            source.parent.mkdir()
            source.write_text("Human Source-2062\n", encoding="utf-8")
            task.write_text(self.task_text(source2059_record=False), encoding="utf-8")
            with (
                patch.object(omo_pending, "SOURCE2062_ROOT", str(root.resolve())),
                patch.object(omo_pending, "SOURCE2062_SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest()),
                patch.object(omo_pending, "ordinary_completion_participant_evidence") as sent,
                self.assertRaisesRegex(BlockingError, "authority changed"),
            ):
                omo_pending.recover_source2062(root, task)
            sent.assert_not_called()

    def test_public_command_uses_authenticated_current_owner(self) -> None:
        args = omo_pending.parse_args(["recover-source2062"])
        self.assertEqual((omo_pending.SOURCE2062_ITEM,), args.items)
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
            "I’ll add a generator comparison to the memo, focused on whether each service can produce 15 qualifying LLM-written blogs per site, what the free trial allows, and the paid cost per site. I’ll distinguish the observed results you supplied from what still needs testing."
        )
        body = omo_completion_email.ordinary_sent_text(BytesParser(policy=policy.default).parsebytes(exact.as_bytes()))
        self.assertEqual(omo_pending.SOURCE2062_BODY_SHA256, hashlib.sha256(body.encode()).hexdigest())
        with (
            patch.dict("os.environ", {"OMO_COMPLETION_SENT_VERIFY_TIMEOUT_S": "0"}),
            patch.object(omo_completion_email, "configured_agent_mail", return_value=settings),
            patch.object(omo_completion_email.imaplib, "IMAP4_SSL", return_value=self.fake_imap(exact)),
        ):
            self.assertIsNotNone(
                omo_pending.ordinary_completion_participant_evidence(
                    omo_pending.SOURCE2062_MESSAGE_ID,
                    omo_pending.SOURCE2062_SUBJECT_SHA256,
                    omo_pending.SOURCE2062_BODY_SHA256,
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
                    omo_pending.SOURCE2062_MESSAGE_ID,
                    omo_pending.SOURCE2062_SUBJECT_SHA256,
                    omo_pending.SOURCE2062_BODY_SHA256,
                )
            )


if __name__ == "__main__":
    unittest.main()
