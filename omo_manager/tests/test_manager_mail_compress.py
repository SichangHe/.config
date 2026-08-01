from __future__ import annotations

import hashlib
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_mail_compress import (
    FULL_FETCH,
    GMAIL_METADATA_FETCH,
    HEADER_FETCH,
    MailRecord,
    accepted_manager_headers,
    cmd_export,
    cmd_mark_seen,
    cmd_trash_superseded,
    ensure_empty_private_dir,
    export_body,
    fetch_gmail_metadata,
    imap_quoted,
    is_manager_record,
    mail_boundary,
    mailbox_exists,
    manager_unread_uids,
    parse_uid_text,
    record_from_msg,
    revalidate_thread_contexts,
    special_use_mailboxes,
    thread_context_digest,
    write_private,
)


class FakeClient:
    def __init__(
        self, uid_results: dict[tuple[str, ...], tuple[str, list[bytes | tuple[bytes, bytes]]] | list[tuple[str, list[bytes | tuple[bytes, bytes]]]]], mailboxes: list[bytes] | None = None
    ) -> None:
        self.uid_results = uid_results
        self.mailboxes = mailboxes if mailboxes is not None else [b'(\\HasNoChildren) "/" "[Gmail]/Trash"']
        self.uid_calls: list[tuple[str, ...]] = []
        self.select_calls: list[tuple[str, bool]] = []
        self.logged_out = False

    def uid(self, *args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
        self.uid_calls.append(args)
        result = self.uid_results.get(args, ("OK", [b""]))
        if isinstance(result, list):
            return result.pop(0) if result else ("OK", [b""])
        return result

    def list(self) -> tuple[str, list[bytes]]:
        return ("OK", self.mailboxes)

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b""]

    def logout(self) -> None:
        self.logged_out = True


class Args:
    def __init__(self, uids: str = "", uid_file: Path | None = None, yes: bool = False) -> None:
        self.uids = uids
        self.uid_file = uid_file
        self.yes = yes


class ExportArgs:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir


class ManagerMailCompressTests(unittest.TestCase):
    @staticmethod
    def raw_message(subject: str, body: str = "body") -> bytes:
        return (f"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nSubject: {subject}\r\nMessage-ID: <one@example.test>\r\n\r\n{body}\r\n").encode()

    @staticmethod
    def gmail_metadata(uid: str, gmail_msgid: str = "100", gmail_thrid: str = "200", flags: str = "", labels: str = r"\Inbox") -> tuple[str, list[bytes]]:
        return "OK", [f"{uid} (FLAGS ({flags}) X-GM-MSGID {gmail_msgid} X-GM-THRID {gmail_thrid} X-GM-LABELS ({labels}))".encode()]

    @staticmethod
    def gmail_mailboxes() -> list[bytes]:
        return [
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"',
            b'(\\HasNoChildren) "/" "[Gmail]/Trash"',
        ]

    @staticmethod
    def write_source_map(parent: Path, record: MailRecord, context_records: list[MailRecord] | None = None) -> Path:
        parent.mkdir()
        thread_digest = thread_context_digest(context_records or [record])
        (parent / "manifest.tsv").write_text(
            "uid\tsource_mailbox\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tbody_bytes\tsubject\n"
            f"{record.uid}\tINBOX\t{record.date}\t{record.gmail_msgid}\t{record.gmail_thrid}\t{record.msgid_sha256}\t{record.raw_sha256}\t{record.flags}\t{record.labels}\t{thread_digest}\t{record.body_bytes}\t{record.subject}\n",
            encoding="utf-8",
        )
        (parent / "mailboxes.tsv").write_text("role\tmailbox\nINBOX\tINBOX\n\\All\t[Gmail]/All Mail\n\\Sent\t[Gmail]/Sent Mail\n", encoding="utf-8")
        uid_file = parent / "superseded-uids.txt"
        uid_file.write_text(f"{record.uid}\n", encoding="utf-8")
        return uid_file

    def test_parse_uid_text_dedupes_and_accepts_commas_or_space(self) -> None:
        self.assertEqual(["5841", "5842", "5843"], parse_uid_text("5841,5842 5841\n5843"))

    def test_parse_uid_text_rejects_non_decimal_uid(self) -> None:
        with self.assertRaises(ValueError):
            parse_uid_text("5841:*")

    def test_manager_record_boundary_requires_self_and_legacy_subject(self) -> None:
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
                MailRecord("1", "", "Agent <agent@example.test>", "Human <human@example.test>", "x", "sha"),
                "agent@example.test",
                "human@example.test",
            )
        )

    def test_manager_record_boundary_excludes_pb_cleanup_subjects(self) -> None:
        for subject in (
            "PB news",
            "Re: PB news setup",
            "[a] PB stock watch: NVDA",
            "Re: [omo_manager] [wl:9] PB urgent",
        ):
            with self.subTest(subject=subject):
                self.assertFalse(
                    is_manager_record(
                        MailRecord("1", "", "Agent <agent@example.test>", "Human <human@example.test>", subject, "sha"),
                        "agent@example.test",
                        "human@example.test",
                    )
                )

        self.assertTrue(
            is_manager_record(
                MailRecord("1", "", "Agent <agent@example.test>", "Human <human@example.test>", "PB newsletter", "sha"),
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

    def test_manager_unread_uids_uses_exact_sender_only_in_split_mode(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        class Client:
            calls: list[tuple[object, ...]] = []

            def uid(self, command: str, *args: object) -> tuple[str, list[bytes]]:
                self.calls.append((command, *args))
                return "OK", [b"7"]

        client = Client()
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()):
            self.assertEqual(["7"], manager_unread_uids(client, "agent@example.test"))
        self.assertEqual(
            [("search", None, "UNSEEN", "FROM", '"agent@example.test"')],
            client.calls,
        )

    def test_manager_unread_uids_uses_legacy_subjects_in_self_addressed_mode(self) -> None:
        class Client:
            calls: list[tuple[object, ...]] = []

            def uid(self, command: str, *args: object) -> tuple[str, list[bytes]]:
                self.calls.append((command, *args))
                return "OK", [b"7"]

        client = Client()
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=None):
            self.assertEqual(["7"], manager_unread_uids(client, "me@example.test"))
        self.assertEqual(
            [
                ("search", None, "UNSEEN", "FROM", '"me@example.test"', "SUBJECT", '"[a]"'),
                ("search", None, "UNSEEN", "FROM", '"me@example.test"', "SUBJECT", '"[omo_manager]"'),
            ],
            client.calls,
        )

    def test_snapshot_and_export_header_filter_keeps_pb_newsletter_only(self) -> None:
        def raw(subject: str) -> bytes:
            return (f"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nSubject: {subject}\r\nMessage-ID: <one@example.test>\r\n\r\n").encode()

        client = FakeClient(
            {
                ("fetch", "1", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"): ("OK", [(b"header", raw("PB newsletter"))]),
                ("fetch", "2", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"): ("OK", [(b"header", raw("PB news"))]),
                ("fetch", "3", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"): ("OK", [(b"header", raw("PB stock watch"))]),
                ("fetch", "4", "(BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)])"): ("OK", [(b"header", raw("PB urgent"))]),
            }
        )

        accepted, skipped = accepted_manager_headers(
            client,  # type: ignore[arg-type]
            ["1", "2", "3", "4"],
            "agent@example.test",
            "human@example.test",
        )

        self.assertEqual(["1"], [record.uid for record in accepted])
        self.assertEqual(["2", "3", "4"], skipped)

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

    def test_fetch_gmail_metadata_keeps_identity_flags_and_labels(self) -> None:
        client = FakeClient({("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7", flags=r"\Flagged", labels=r"\Inbox \Important")})
        self.assertEqual(("100", "200", r"\Flagged", r"\Inbox \Important"), fetch_gmail_metadata(client, "7"))

    def test_export_writes_gmail_context_and_special_mailboxes(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        other_raw = b"From: Other <other@example.test>\r\nTo: Human <human@example.test>\r\nSubject: [worker:0] complete\r\nMessage-ID: <two@example.test>\r\n\r\nother context\r\n"
        client = FakeClient(
            {
                ("search", None, "UNSEEN", "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70 71"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", other_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
            patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
        ):
            self.assertEqual(0, cmd_export(ExportArgs(Path(tmp) / "export")))
            manifest = (Path(tmp) / "export" / "manifest.tsv").read_text(encoding="utf-8")
            mailboxes = (Path(tmp) / "export" / "mailboxes.tsv").read_text(encoding="utf-8")
            self.assertIn("gmail_thrid", manifest)
            self.assertIn("\t200\t", manifest)
            self.assertIn("[Gmail]/All Mail", mailboxes)
            self.assertTrue((Path(tmp) / "export" / "threads" / "200-100.txt").exists())
            self.assertIn("From: Other <other@example.test>", (Path(tmp) / "export" / "threads" / "200-101.txt").read_text(encoding="utf-8"))
            self.assertIn("To: Human <human@example.test>", (Path(tmp) / "export" / "threads" / "200-101.txt").read_text(encoding="utf-8"))
            self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)

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

    def test_special_use_mailboxes_preserve_modified_utf7_wire_name(self) -> None:
        client = FakeClient(
            {},
            [
                b'(\\HasNoChildren \\All) "/" "&ZeVnLIqe-"',
                b'(\\HasNoChildren \\Sent) "/" "&ZeVnLIqe-/Sent"',
            ],
        )
        self.assertEqual({r"\All": "&ZeVnLIqe-", r"\Sent": "&ZeVnLIqe-/Sent"}, special_use_mailboxes(client))

    def test_revalidation_refuses_conflicting_digest_for_one_thread(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        second_raw = self.raw_message("[worker:0] complete", "second").replace(b"<one@example.test>", b"<two@example.test>")
        client = FakeClient(
            {
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70 71"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", second_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            }
        )
        source_map = {
            "7": {"gmail_thrid": "200", "gmail_msgid": "100", "thread_context_sha256": "first"},
            "8": {"gmail_thrid": "200", "gmail_msgid": "101", "thread_context_sha256": "second"},
        }
        self.assertFalse(revalidate_thread_contexts(client, "[Gmail]/All Mail", source_map, "agent@example.test", "human@example.test"))

    def test_trash_superseded_requires_yes(self) -> None:
        self.assertEqual(2, cmd_trash_superseded(Args(uids="7")))

    def test_mark_seen_is_retired_for_compression(self) -> None:
        self.assertEqual(2, cmd_mark_seen(Args(uids="7", yes=True)))

    def test_trash_superseded_requires_private_source_map(self) -> None:
        self.assertEqual(2, cmd_trash_superseded(Args(uids="7", yes=True)))

    def test_trash_superseded_moves_only_revalidated_source(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<one@example.test>").hexdigest()[:12],
            "body\n",
            "100",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(raw).hexdigest(),
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
                ("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""]),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record)
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(0, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)
        self.assertTrue(client.logged_out)

    def test_trash_superseded_refuses_changed_source_content(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<one@example.test>").hexdigest()[:12],
            "body\n",
            "100",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(raw).hexdigest(),
        )
        changed = self.raw_message("[worker:0] complete", "changed")
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", changed)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record)
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_refuses_changed_thread_context(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<one@example.test>").hexdigest()[:12],
            "body\n",
            "100",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(raw).hexdigest(),
        )
        context_changed = self.raw_message("[worker:0] complete", "context changed")
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): [("OK", [b"70"]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", context_changed)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record)
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_retains_flagged_source(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<one@example.test>").hexdigest()[:12],
            "body\n",
            "100",
            "200",
            r"\Flagged",
            r"\Inbox",
            hashlib.sha256(raw).hexdigest(),
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7", flags=r"\Flagged"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record)
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_retains_thread_with_other_context_member(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<one@example.test>").hexdigest()[:12],
            "body\n",
            "100",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(raw).hexdigest(),
        )
        other_raw = b"From: Other <other@example.test>\r\nTo: Human <human@example.test>\r\nSubject: [worker:0] complete\r\nMessage-ID: <two@example.test>\r\n\r\nother context\r\n"
        other_record = MailRecord(
            "71",
            "date",
            "Other <other@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<two@example.test>").hexdigest()[:12],
            "other context\n",
            "101",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(other_raw).hexdigest(),
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70 71"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", other_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record, [record, other_record])
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_refuses_pb_cleanup_subject(self) -> None:
        raw = self.raw_message("PB urgent")
        record = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "PB urgent",
            hashlib.sha256(b"<one@example.test>").hexdigest()[:12],
            "body\n",
            "100",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(raw).hexdigest(),
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record)
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)


if __name__ == "__main__":
    unittest.main()
