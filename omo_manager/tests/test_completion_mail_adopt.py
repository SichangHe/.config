from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_completion_mail_adopt import AdoptionBinding, Args, MESSAGE_ID, OUTCOME, ProviderBinding, TASK_TARGET, THREAD_ROOT_MESSAGE_ID, adopt, exact_special_use_mailboxes, observe_provider_delivery, receipt, unique_message_uids, validate_task
from omo_manager.omo_email_config import AgentMailSettings


ITEMS = (
    "Find me a good transcription software by searching online using the multiple tools we have.",
    "The transcription software should give timeline, distinguish multiple speakers and be very accurate.",
    "It can be a command line tool, must be open source, and it could be a user graphics interface tool supporting Mac OS and Linux.",
)


def task_text(
    *, status: str = "blocked", target: str = TASK_TARGET, manager: str = "wl:1", blocker: str = "owner-authenticated reconciliation of already delivered completion email; no resend", is_manager: bool = False, items: tuple[str, ...] = ITEMS
) -> str:
    queue = "pending_task_items: []\n" if not items else "pending_task_items:\n" + "".join(f"  - {item}\n" for item in items)
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"blocked_on: {blocker}\n"
        f"runat: {target}\n"
        "tool: codex\n"
        f"managerat: {manager}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        f"{queue}"
        "---\n"
    )


def delivered_message(
    *, message_id: str = MESSAGE_ID, subject: str = "Re: [wl:32] transcription software", parent: str = THREAD_ROOT_MESSAGE_ID, body: str = "report\n"
) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["In-Reply-To"] = parent
    message["References"] = parent
    message["Subject"] = subject
    message["From"] = "agent@example.test"
    message["To"] = "human@example.test"
    message.set_content(body)
    return message.as_bytes()


class FakeClient:
    def __init__(self, thread_response: list[bytes] | None = None) -> None:
        self.mailbox = ""
        self.thread_response = thread_response or [b"1 2"]

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.mailbox = mailbox.strip('"')
        return "OK", [b""]

    def login(self, _address: str, _password: str) -> tuple[str, list[bytes]]:
        return "OK", [b""]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]

    def list(self) -> tuple[str, list[bytes]]:
        return "OK", [b'(\\All) "/" "all"', b'(\\Sent) "/" "sent"']

    def uid(self, command: str, *args: str | None) -> tuple[str, list[bytes]]:
        if command == "search" and args[1:3] == ("X-GM-THRID", "200"):
            return "OK", self.thread_response
        raise AssertionError((command, args))


class CompletionMailAdoptTest(unittest.TestCase):
    def binding(self, raw: bytes) -> AdoptionBinding:
        body = b"report\n"
        return AdoptionBinding(
            "a" * 64,
            "100",
            "200",
            "300",
            hashlib.sha256(raw).hexdigest(),
            hashlib.sha256(body).hexdigest(),
            "Re: [wl:32] transcription software",
            (THREAD_ROOT_MESSAGE_ID, MESSAGE_ID),
            "12",
            "5",
        )

    def provider(self, raw: bytes) -> ProviderBinding:
        return ProviderBinding("1", "12", "9", "5", "100", "200", "300", hashlib.sha256(raw).hexdigest(), hashlib.sha256(b"report\n").hexdigest(), (THREAD_ROOT_MESSAGE_ID, MESSAGE_ID))

    def observe(
        self,
        raw: bytes,
        binding: AdoptionBinding | None = None,
        *,
        sent_raw: bytes | None = None,
        sent_metadata: tuple[str, str, str] | None = None,
        thread_response: list[bytes] | None = None,
    ) -> ProviderBinding:
        client = FakeClient(thread_response)
        raw_by_uid = {"1": raw, "2": delivered_message(message_id=THREAD_ROOT_MESSAGE_ID, subject="[wl:1] transcription software"), "9": sent_raw or raw}
        metadata_by_uid = {"1": ("100", "200", "300"), "2": ("90", "200", "250"), "9": sent_metadata or ("100", "200", "300")}
        with patch("omo_manager.omo_completion_mail_adopt.unique_message_uids", side_effect=lambda _client, mailbox, _message_id: ("1",) if mailbox == "all" else ("9",)), patch(
            "omo_manager.omo_completion_mail_adopt.selected_uidvalidity", side_effect=("12", "5")
        ), patch("omo_manager.omo_completion_mail_adopt.fetch_msg_bytes", side_effect=lambda _client, uid, _fetch: raw_by_uid[uid]), patch(
            "omo_manager.omo_completion_mail_adopt.metadata", side_effect=lambda _client, uid: metadata_by_uid[uid]
        ):
            return observe_provider_delivery(client, AgentMailSettings("agent@example.test", "password", "human@example.test"), binding or self.binding(raw))  # pyright: ignore[reportArgumentType]

    def test_success_binds_exact_sent_thread_and_receipt_has_no_resend_policy(self) -> None:
        raw = delivered_message()
        provider = self.observe(raw)
        self.assertEqual((THREAD_ROOT_MESSAGE_ID, MESSAGE_ID), provider.thread_message_ids)
        args = Args(Path("/root"), "a" * 64, self.binding(raw), ITEMS, Path("/receipt"))
        record = json.loads(receipt(args, provider))
        self.assertEqual("already-delivered-no-resend", record["mail_policy"])
        self.assertEqual(list(ITEMS), record["pending_task_items"])
        self.assertEqual(OUTCOME, record["outcome"])

    def test_provider_rejects_missing_ambiguous_and_cross_mailbox_delivery(self) -> None:
        raw = delivered_message()
        for all_uids, sent_uids, error in (((), ("9",), "missing or ambiguous"), (("1", "2"), ("9",), "missing or ambiguous"), (("1",), (), "missing or ambiguous")):
            with self.subTest(all_uids=all_uids, sent_uids=sent_uids), patch(
                "omo_manager.omo_completion_mail_adopt.unique_message_uids", side_effect=lambda _client, mailbox, _message_id: all_uids if mailbox == "all" else sent_uids
            ), patch(
                "omo_manager.omo_completion_mail_adopt.selected_uidvalidity", side_effect=("12", "5")
            ), self.assertRaisesRegex(OSError, error):
                observe_provider_delivery(FakeClient(), AgentMailSettings("agent@example.test", "password", "human@example.test"), self.binding(raw))  # pyright: ignore[reportArgumentType]
        client = FakeClient()
        with patch.object(client, "list", return_value=("OK", [b'(\\All \\Sent) "/" "same"'])), self.assertRaisesRegex(OSError, "exactly one distinct"):
            observe_provider_delivery(client, AgentMailSettings("agent@example.test", "password", "human@example.test"), self.binding(raw))  # pyright: ignore[reportArgumentType]

    def test_provider_rejects_duplicate_special_use_mailboxes(self) -> None:
        client = FakeClient()
        duplicate_lists = (
            [b'(\\All) "/" "all-a"', b'(\\All) "/" "all-b"', b'(\\Sent) "/" "sent"'],
            [b'(\\All) "/" "all"', b'(\\Sent) "/" "sent-a"', b'(\\Sent) "/" "sent-b"'],
        )
        for response in duplicate_lists:
            with self.subTest(response=response), patch.object(client, "list", return_value=("OK", response)), self.assertRaisesRegex(OSError, "exactly one distinct"):
                exact_special_use_mailboxes(client)  # pyright: ignore[reportArgumentType]

    def test_provider_rejects_multi_entry_message_and_thread_search_responses(self) -> None:
        client = FakeClient()
        with patch("omo_manager.omo_completion_mail_adopt.imap_uid", return_value=("OK", [b"1", b"2"])), self.assertRaisesRegex(
            OSError, "ambiguous response shape"
        ):
            unique_message_uids(client, "all", MESSAGE_ID)  # pyright: ignore[reportArgumentType]
        raw = delivered_message()
        with self.assertRaisesRegex(OSError, "ambiguous response shape"):
            self.observe(raw, thread_response=[b"1", b"2"])

    def test_provider_rejects_thread_header_body_and_provider_binding_drift(self) -> None:
        raw = delivered_message()
        cases = (
            (delivered_message(parent="<wrong@example.test>"), self.binding(delivered_message(parent="<wrong@example.test>"))),
            (delivered_message(subject="wrong"), self.binding(delivered_message(subject="wrong"))),
            (raw, replace(self.binding(raw), gmail_thread_id="201")),
            (raw, replace(self.binding(raw), body_sha256="0" * 64)),
        )
        for changed_raw, changed_binding in cases:
            with self.subTest(binding=changed_binding), self.assertRaises(OSError):
                self.observe(changed_raw, changed_binding)

    def test_provider_rejects_exactly_one_all_and_sent_message_with_different_raw_or_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "transcription_sw.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            output = root / "receipt.json"
            raw = delivered_message()
            for kwargs in (
                {"sent_raw": delivered_message(body="different\n")},
                {"sent_metadata": ("101", "200", "300")},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(OSError, "do not identify the same delivered message"):
                    self.observe(raw, **kwargs)  # pyright: ignore[reportArgumentType]
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertFalse(output.exists())

    def test_task_rejects_digest_status_target_type_and_ordered_queue_drift(self) -> None:
        for case in ("digest", "status", "target", "manager", "blocker", "type", "queue"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                text = task_text(
                    status="running" if case == "status" else "blocked",
                    target="wl:31" if case == "target" else TASK_TARGET,
                    manager="wl:2" if case == "manager" else "wl:1",
                    blocker="other" if case == "blocker" else "owner-authenticated reconciliation of already delivered completion email; no resend",
                    is_manager=case == "type",
                    items=ITEMS[:-1] if case == "queue" else ITEMS,
                )
                (root / "transcription_sw.md").write_text(text, encoding="utf-8")
                digest = "0" * 64 if case == "digest" else hashlib.sha256(text.encode()).hexdigest()
                with self.assertRaises(OSError):
                    validate_task(root, digest, ITEMS)

    def test_adopt_writes_one_private_receipt_and_never_mutates_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "private"
            output_dir.mkdir(mode=0o700)
            task = root / "transcription_sw.md"
            text = task_text()
            task.write_text(text, encoding="utf-8")
            raw = delivered_message()
            binding = replace(self.binding(raw), task_sha256=hashlib.sha256(text.encode()).hexdigest())
            args = Args(root, binding.task_sha256, binding, ITEMS, output_dir / "receipt.json")
            with patch("omo_manager.omo_completion_mail_adopt.configured_agent_mail", return_value=AgentMailSettings("agent@example.test", "password", "human@example.test")), patch(
                "omo_manager.omo_completion_mail_adopt.imaplib.IMAP4_SSL", return_value=FakeClient()
            ), patch("omo_manager.omo_completion_mail_adopt.observe_provider_delivery", return_value=self.provider(raw)):
                adopt(args)
                with self.assertRaises(FileExistsError):
                    adopt(args)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(0o600, args.output.stat().st_mode & 0o777)

    def test_adopt_rejects_provider_mutation_before_receipt_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "private"
            output_dir.mkdir(mode=0o700)
            text = task_text()
            task = root / "transcription_sw.md"
            task.write_text(text, encoding="utf-8")
            raw = delivered_message()
            binding = replace(self.binding(raw), task_sha256=hashlib.sha256(text.encode()).hexdigest())
            args = Args(root, binding.task_sha256, binding, ITEMS, output_dir / "receipt.json")
            changed = replace(self.provider(raw), internaldate_unix_ms="301")
            with patch("omo_manager.omo_completion_mail_adopt.configured_agent_mail", return_value=AgentMailSettings("agent@example.test", "password", "human@example.test")), patch(
                "omo_manager.omo_completion_mail_adopt.imaplib.IMAP4_SSL", return_value=FakeClient()
            ), patch("omo_manager.omo_completion_mail_adopt.observe_provider_delivery", side_effect=(self.provider(raw), changed)), self.assertRaisesRegex(
                OSError, "changed during adoption"
            ):
                adopt(args)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
