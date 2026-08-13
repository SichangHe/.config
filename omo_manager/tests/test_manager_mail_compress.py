from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_mail_compress import (
    FULL_FETCH,
    GMAIL_METADATA_FETCH,
    HEADER_FETCH,
    ImapOperationError,
    MailRecord,
    accepted_manager_headers,
    claim_batch,
    cmd_reconcile_intent,
    cmd_recover_already_trashed,
    cmd_retain_thread,
    cmd_export,
    cmd_identity_preflight,
    cmd_mark_seen,
    cmd_trash_superseded,
    cmd_verify_run,
    connect_mailbox,
    ensure_empty_private_dir,
    export_receipt_path,
    export_failure_diagnostics,
    export_body,
    export_batches,
    fetch_msg_bytes,
    fetch_gmail_metadata,
    imap_operation,
    imap_quoted,
    intent_reconciliation_evidence,
    is_manager_record,
    mail_boundary,
    mailbox_exists,
    manager_candidate_uids,
    parse_uid_text,
    prepare_thread_disposition,
    record_from_msg,
    record_matches_reconciliation_location,
    replacement_exists,
    special_use_mailboxes,
    frozen_thread_context,
    thread_context_digest,
    tsv_value,
    verify_post_move_imap,
    verified_existing_trash_records,
    write_export_receipt,
    write_private,
)


class FakeClient:
    def __init__(
        self,
        uid_results: dict[tuple[str, ...], tuple[str, list[bytes | tuple[bytes, bytes]]] | list[tuple[str, list[bytes | tuple[bytes, bytes]]]]],
        mailboxes: list[bytes] | None = None,
        capabilities: list[bytes] | None = None,
        uidvalidity: str = "9",
    ) -> None:
        self.uid_results = uid_results
        self.mailboxes = mailboxes if mailboxes is not None else [b'(\\HasNoChildren) "/" "[Gmail]/Trash"']
        self.capabilities = capabilities if capabilities is not None else [b"IMAP4rev1 X-GM-EXT-1"]
        self.uidvalidity = uidvalidity
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

    def capability(self) -> tuple[str, list[bytes]]:
        return "OK", self.capabilities

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.select_calls.append((mailbox, readonly))
        return "OK", [b""]

    def response(self, _name: str) -> tuple[str, list[bytes]]:
        return "UIDVALIDITY", [self.uidvalidity.encode()]

    def logout(self) -> None:
        self.logged_out = True


class Args:
    def __init__(self, uids: str = "", uid_file: Path | None = None, yes: bool = False) -> None:
        self.uids = uids
        self.uid_file = uid_file
        self.yes = yes
        self.source_dir = uid_file.parent if uid_file is not None else Path(".")
        self.batch_id = "batch-0001"
        self.owner = "reviewer-1"
        self.gmail_thrid = "200"
        self.reason_file = self.source_dir / "reason.txt"
        self.task_evidence_file = self.source_dir / "task-evidence.txt"
        self.replacement_id = ""
        self.replacement_not_required = True


class ExportArgs:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.threads_per_batch = 10


class RetainArgs:
    def __init__(self, source_dir: Path, gmail_thrid: str = "200") -> None:
        self.source_dir = source_dir
        self.batch_id = "batch-0001"
        self.owner = "reviewer-1"
        self.gmail_thrid = gmail_thrid
        self.reason_file = source_dir / "reason.txt"
        self.task_evidence_file = source_dir / "task-evidence.txt"


class VerifyArgs:
    def __init__(self, source_dir: Path) -> None:
        self.source_dir = source_dir


class ReconcileArgs:
    def __init__(self, source_dir: Path, gmail_thrid: str = "200") -> None:
        self.source_dir = source_dir
        self.gmail_thrid = gmail_thrid


class ManagerMailCompressTests(unittest.TestCase):
    def test_deployed_python_310_compiles_imports_and_bounds_imap_operation(self) -> None:
        root = Path(__file__).parents[2]
        helper = root / "omo_manager" / "omo_manager_mail_compress.py"
        code = """
import py_compile
import os
import socket
import sys
import threading
import tempfile
from unittest.mock import patch

py_compile.compile(os.environ["HELPER"], doraise=True)
assert sys.version_info[:3] == (3, 10, 12), sys.version
from pathlib import Path
from omo_manager.omo_manager_mail_compress import ImapOperationError, export_receipt_path, imap_operation, write_export_receipt

class Socket:
    def __init__(self):
        self.closed = threading.Event()

    def shutdown(self, how):
        assert how == socket.SHUT_RDWR
        self.closed.set()

    def close(self):
        self.closed.set()

class Client:
    def __init__(self):
        self.sock = Socket()

client = Client()
assert imap_operation(client, "success", lambda: 7) == 7
release = threading.Event()
with patch("omo_manager.omo_manager_mail_compress.IMAP_OPERATION_TIMEOUT_S", 0.01):
    try:
        imap_operation(client, "bounded", release.wait)
    except ImapOperationError as exc:
        assert "timed out: stage=bounded timeout_s=0.01" in str(exc)
    else:
        raise AssertionError("bounded operation did not time out")
release.set()
assert client.sock.closed.is_set()
with tempfile.TemporaryDirectory() as tmp:
    out_dir = Path(tmp) / "run"
    write_export_receipt(out_dir, "success", "complete", "none")
    receipt = export_receipt_path(out_dir)
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert "\\tsuccess\\tcomplete\\tnone\\tnone\\n" in receipt.read_text()
"""
        env = os.environ.copy()
        env["HELPER"] = str(helper)
        result = subprocess.run(
            ["/usr/bin/python3.10", "-c", code],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_direct_executable_starts_with_deployed_python(self) -> None:
        helper = Path(__file__).parents[1] / "omo_manager_mail_compress.py"
        result = subprocess.run([helper, "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_export_timeout_diagnostics_redact_complete_mailbox_name(self) -> None:
        exc = ImapOperationError("select mailbox=Projects Alice Secret", "IMAP operation timed out")
        self.assertEqual(
            ("imap-timeout", "select mailbox=<redacted>", "deadline-expired"),
            export_failure_diagnostics(exc, "freeze-candidates"),
        )

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
    def export_receipt(out_dir: Path) -> str:
        return export_receipt_path(out_dir).read_text(encoding="utf-8")

    @staticmethod
    def write_source_map(parent: Path, record: MailRecord, context_records: list[MailRecord] | None = None) -> Path:
        parent.mkdir()
        thread_digest = thread_context_digest(context_records or [record])
        (parent / "manifest.tsv").write_text(
            "uid\tsource_mailbox\tuidvalidity\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tbody_bytes\tsubject\n"
            f"{record.uid}\tINBOX\t9\t{record.date}\t{record.gmail_msgid}\t{record.gmail_thrid}\t{record.msgid_sha256}\t{record.raw_sha256}\t{record.flags}\t{record.labels}\t{thread_digest}\t{record.body_bytes}\t{record.subject}\n",
            encoding="utf-8",
        )
        (parent / "mailboxes.tsv").write_text("role\tmailbox\nINBOX\tINBOX\n\\All\t[Gmail]/All Mail\n\\Sent\t[Gmail]/Sent Mail\n", encoding="utf-8")
        (parent / "batches.tsv").write_text(
            f"batch_id\tgmail_thrid\tuid\tgmail_msgid\tsubject\tbody_file\nbatch-0001\t{record.gmail_thrid}\t{record.uid}\t{record.gmail_msgid}\t{record.subject}\t{record.uid}.txt\n",
            encoding="utf-8",
        )
        (parent / "run.tsv").write_text("fixed_start_utc\tsource_count\tthread_count\tthreads_per_batch\n2026-08-09T00:00:00+00:00\t1\t1\t10\n", encoding="utf-8")
        context_lines = ["gmail_thrid\tgmail_msgid\tmsgid_sha256\traw_sha256\tflags\tlabels\tscope\tsender\trecipient\tall_mailbox_uid\tbody_bytes"]
        for context in context_records or [record]:
            context_lines.append(
                f"{context.gmail_thrid}\t{context.gmail_msgid}\t{context.msgid_sha256}\t{context.raw_sha256}\t{tsv_value(context.flags)}\t{tsv_value(context.labels)}\tmanager-to-human\t{context.sender}\t{context.to}\t{context.uid}\t{context.body_bytes}"
            )
        (parent / "thread-context.tsv").write_text("\n".join(context_lines) + "\n", encoding="utf-8")
        (parent / "threads").mkdir()
        for context in context_records or [record]:
            (parent / "threads" / f"{context.gmail_thrid}-{context.gmail_msgid}.txt").write_text(
                export_body(context, include_addresses=True), encoding="utf-8"
            )
        (parent / "thread-digests.tsv").write_text(
            f"gmail_thrid\tthread_context_sha256\n{record.gmail_thrid}\t{thread_digest}\n",
            encoding="utf-8",
        )
        for name in ("claims", "intents", "outcomes", "recoveries"):
            (parent / name).mkdir()
        (parent / "reason.txt").write_text("irrelevant after task review\n", encoding="utf-8")
        (parent / "task-evidence.txt").write_text("task complete\n", encoding="utf-8")
        claim_batch(parent, "batch-0001", "reviewer-1")
        uid_file = parent / "superseded-uids.txt"
        uid_file.write_text(f"{record.uid}\n", encoding="utf-8")
        return uid_file

    def test_frozen_thread_context_preserves_legacy_outer_label_quotes(self) -> None:
        digest = "1cd8aa8dc65a86f86fc2d10193aafd1262f3aa865b83364819a2a78f1c9e1e8c"
        with tempfile.TemporaryDirectory() as temp_dir:
            source_dir = Path(temp_dir) / "source"
            source_dir.mkdir()
            (source_dir / "threads").mkdir()
            (source_dir / "thread-context.tsv").write_text(
                "gmail_thrid\tgmail_msgid\tmsgid_sha256\traw_sha256\tflags\tlabels\n"
                '200\t100\tmsg-a\traw-a\t\\Seen\t"\\Inbox Important Mail"\n'
                '200\t101\tmsg-b\traw-b\t\t"\\Inbox"\n',
                encoding="utf-8",
            )
            (source_dir / "thread-digests.tsv").write_text(
                f"gmail_thrid\tthread_context_sha256\n200\t{digest}\n", encoding="utf-8"
            )
            (source_dir / "manifest.tsv").write_text(
                f"gmail_thrid\tthread_context_sha256\n200\t{digest}\n", encoding="utf-8"
            )
            first_export = (
                "Message-ID-SHA256: msg-a\n"
                "Gmail-Message-ID: 100\n"
                "Gmail-Thread-ID: 200\n"
                "Flags: \\Seen\n"
                'Labels: "\\Inbox Important Mail"\n'
                "Raw-SHA256: raw-a\n\nbody\n"
            )
            second_export = (
                "Message-ID-SHA256: msg-b\n"
                "Gmail-Message-ID: 101\n"
                "Gmail-Thread-ID: 200\n"
                "Flags: \n"
                'Labels: "\\Inbox"\n'
                "Raw-SHA256: raw-b\n\nbody\n"
            )
            first_path = source_dir / "threads" / "200-100.txt"
            first_path.write_text(first_export, encoding="utf-8")
            (source_dir / "threads" / "200-101.txt").write_text(second_export, encoding="utf-8")
            frozen = frozen_thread_context(source_dir, "200")

            self.assertEqual(23, len(frozen["100"]["labels"]))
            self.assertEqual(8, len(frozen["101"]["labels"]))

            first_path.write_text(first_export.replace("Flags: \\Seen", "Flags: \\Flagged"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "evidence files disagree"):
                frozen_thread_context(source_dir, "200")
            first_path.write_text(first_export, encoding="utf-8")
            (source_dir / "thread-digests.tsv").write_text(
                "gmail_thrid\tthread_context_sha256\n200\tcorrupt\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "digest binding failed"):
                frozen_thread_context(source_dir, "200")

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

    def test_manager_candidate_uids_uses_exact_sender_without_signal_filter_in_split_mode(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        class Client:
            calls: list[tuple[object, ...]] = []

            def uid(self, command: str, *args: object) -> tuple[str, list[bytes]]:
                self.calls.append((command, *args))
                return "OK", [b"7" if args == (None, "ALL") else b"7 8"]

        client = Client()
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()):
            self.assertEqual(["7"], manager_candidate_uids(client, "agent@example.test"))
        self.assertEqual(
            [("search", None, "ALL"), ("search", None, "FROM", '"agent@example.test"')],
            client.calls,
        )

    def test_manager_candidate_uids_uses_legacy_subjects_in_self_addressed_mode(self) -> None:
        class Client:
            calls: list[tuple[object, ...]] = []

            def uid(self, command: str, *args: object) -> tuple[str, list[bytes]]:
                self.calls.append((command, *args))
                return "OK", [b"7"]

        client = Client()
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=None):
            self.assertEqual(["7"], manager_candidate_uids(client, "me@example.test"))
        self.assertEqual(
            [
                ("search", None, "ALL"),
                ("search", None, "FROM", '"me@example.test"', "SUBJECT", '"[a]"'),
                ("search", None, "FROM", '"me@example.test"', "SUBJECT", '"[omo_manager]"'),
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

    def test_fetch_gmail_metadata_ignores_identity_text_inside_labels(self) -> None:
        client = FakeClient(
            {
                ("fetch", "7", GMAIL_METADATA_FETCH): (
                    "OK",
                    [b'7 (FLAGS () X-GM-LABELS ("x X-GM-MSGID 100 X-GM-THRID 200 y"))'],
                )
            }
        )
        self.assertEqual(("", ""), fetch_gmail_metadata(client, "7")[:2])

    def test_fetch_timeout_aborts_blocked_uid_fetch_without_retry(self) -> None:
        class BlockingSocket:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def shutdown(self, _how: int) -> None:
                self.closed.set()

            def close(self) -> None:
                self.closed.set()

        class BlockingClient:
            def __init__(self) -> None:
                self.sock = BlockingSocket()
                self.uid_calls: list[tuple[str, ...]] = []
                self.release = threading.Event()

            def uid(self, *args: str) -> tuple[str, list[bytes]]:
                self.uid_calls.append(args)
                self.release.wait()
                raise OSError("stub socket aborted")

        client = BlockingClient()
        with (
            patch("omo_manager.omo_manager_mail_compress.IMAP_OPERATION_TIMEOUT_S", 0.01),
            self.assertRaisesRegex(RuntimeError, r"timed out: stage=message-fetch uid=7 timeout_s=0.01"),
        ):
            fetch_msg_bytes(client, "7", FULL_FETCH, n_attempts=2)  # type: ignore[arg-type]

        client.release.set()
        self.assertEqual([("fetch", "7", FULL_FETCH)], client.uid_calls)
        self.assertTrue(client.sock.closed.is_set())

        with self.assertRaisesRegex(RuntimeError, "client is unusable after timeout: stage=second-operation"):
            imap_operation(client, "second-operation", lambda: self.fail("dead client operation ran"))  # type: ignore[arg-type]

    def test_connect_timeout_returns_and_closes_late_client(self) -> None:
        release = threading.Event()

        class LateClient:
            def __init__(self, _host: str, timeout: float) -> None:
                self.timeout = timeout
                self.shutdown_called = threading.Event()

            def shutdown(self) -> None:
                self.shutdown_called.set()

        clients: list[LateClient] = []

        def construct(host: str, timeout: float) -> LateClient:
            client = LateClient(host, timeout)
            clients.append(client)
            release.wait()
            return client

        with (
            patch("omo_manager.omo_manager_mail_compress.imaplib.IMAP4_SSL", side_effect=construct),
            patch("omo_manager.omo_manager_mail_compress.IMAP_OPERATION_TIMEOUT_S", 0.01),
            self.assertRaisesRegex(RuntimeError, r"timed out: stage=connect timeout_s=0.01"),
        ):
            connect_mailbox("imap.example.test")

        release.set()
        for _attempt in range(100):
            if clients and clients[0].shutdown_called.wait(0.01):
                break
        self.assertEqual(1, len(clients))
        self.assertEqual(0.01, clients[0].timeout)
        self.assertTrue(clients[0].shutdown_called.is_set())

    def test_replacement_timeout_does_not_restore_mailbox_on_dead_client(self) -> None:
        client = FakeClient({})

        def timeout(*_args: object) -> tuple[str, list[bytes]]:
            setattr(client, "_omo_operation_timed_out", True)
            raise RuntimeError("stub timeout")

        with patch("omo_manager.omo_manager_mail_compress.imap_uid", side_effect=timeout), self.assertRaisesRegex(RuntimeError, "stub timeout"):
            replacement_exists(client, "Sent", "<replacement@example.test>", "agent@example.test", "human@example.test")  # type: ignore[arg-type]
        self.assertEqual([('"Sent"', True)], client.select_calls)

    def test_trash_verification_timeout_does_not_restore_mailbox_on_dead_client(self) -> None:
        client = FakeClient({})

        def timeout(*_args: object) -> tuple[str, list[bytes]]:
            setattr(client, "_omo_operation_timed_out", True)
            raise RuntimeError("stub timeout")

        source_map = {"7": {"gmail_msgid": "100"}}
        with patch("omo_manager.omo_manager_mail_compress.imap_uid", side_effect=timeout), self.assertRaisesRegex(RuntimeError, "stub timeout"):
            verified_existing_trash_records(client, source_map, "agent@example.test", "human@example.test")  # type: ignore[arg-type]
        self.assertEqual([('"[Gmail]/Trash"', True)], client.select_calls)

    def test_identity_preflight_uses_existing_imap_authentication(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        output = io.StringIO()
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
            patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            redirect_stdout(output),
        ):
            self.assertEqual(0, cmd_identity_preflight(Args()))
        self.assertIn("gmail_imap_extension=1", output.getvalue())
        self.assertIn("unique_identity_count=1", output.getvalue())
        self.assertIn("complete_thread_count=1", output.getvalue())
        self.assertIn("gate=pass", output.getvalue())

    def test_identity_preflight_fetch_timeout_names_stage_and_blocks(self) -> None:
        raw = self.raw_message("[worker:0] complete")

        class BlockingSocket:
            def shutdown(self, _how: int) -> None:
                pass

            def close(self) -> None:
                pass

        class BlockingFetchClient(FakeClient):
            def __init__(self) -> None:
                super().__init__(
                    {
                        ("search", None, "ALL"): ("OK", [b"7"]),
                        ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                        ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                    }
                )
                self.sock = BlockingSocket()
                self.release = threading.Event()

            def uid(self, *args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if args == ("fetch", "7", FULL_FETCH):
                    self.uid_calls.append(args)
                    self.release.wait()
                    raise OSError("stub remains blocked past socket close")
                return super().uid(*args)

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        client = BlockingFetchClient()
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
            patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            patch("omo_manager.omo_manager_mail_compress.IMAP_OPERATION_TIMEOUT_S", 0.01),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            self.assertEqual(1, cmd_identity_preflight(Args()))

        client.release.set()
        self.assertIn("gate=block", output.getvalue())
        self.assertIn("failed_stage=message-fetch uid=7", errors.getvalue())
        self.assertEqual(1, client.uid_calls.count(("fetch", "7", FULL_FETCH)))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_export_writes_gmail_context_and_special_mailboxes(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        other_raw = b"From: Other <other@example.test>\r\nTo: Human <human@example.test>\r\nSubject: [worker:0] complete\r\nMessage-ID: <two@example.test>\r\n\r\nother context\r\n"
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
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
            out_dir = Path(tmp) / "export"
            self.assertEqual(0, cmd_export(ExportArgs(out_dir)))
            manifest = (out_dir / "manifest.tsv").read_text(encoding="utf-8")
            mailboxes = (out_dir / "mailboxes.tsv").read_text(encoding="utf-8")
            self.assertIn("gmail_thrid", manifest)
            self.assertIn("\t200\t", manifest)
            self.assertIn("[Gmail]/All Mail", mailboxes)
            self.assertIn("fixed_start_utc", (out_dir / "run.tsv").read_text(encoding="utf-8"))
            self.assertIn("batch-0001\t200\t7", (out_dir / "batches.tsv").read_text(encoding="utf-8"))
            self.assertTrue((out_dir / "threads" / "200-100.txt").exists())
            self.assertIn("From: Other <other@example.test>", (out_dir / "threads" / "200-101.txt").read_text(encoding="utf-8"))
            self.assertIn("To: Human <human@example.test>", (out_dir / "threads" / "200-101.txt").read_text(encoding="utf-8"))
            receipt = self.export_receipt(out_dir)
            self.assertIn("\tsuccess\tcomplete\tnone\tnone\n", receipt)
            self.assertNotIn(str(out_dir), receipt)
            self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)

    def test_export_retries_missing_full_fetch_for_only_the_frozen_uid(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_FETCH): [("OK", [b""]), ("OK", [(b"message", raw)])],
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
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
            out_dir = Path(tmp) / "export"
            self.assertEqual(0, cmd_export(ExportArgs(out_dir)))
            self.assertTrue((out_dir / "manifest.tsv").exists())
        self.assertEqual(1, client.uid_calls.count(("search", None, "ALL")))
        self.assertEqual(2, client.uid_calls.count(("fetch", "7", FULL_FETCH)))
        self.assertFalse(any(call[:2] == ("fetch", "8") for call in client.uid_calls))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_export_fails_without_manifest_after_bounded_missing_full_fetch(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_FETCH): [("OK", [b""]), ("OK", [b""])],
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
            out_dir = Path(tmp) / "export"
            with self.assertRaisesRegex(RuntimeError, "no usable record: uid=7"):
                cmd_export(ExportArgs(out_dir))
            self.assertFalse((out_dir / "manifest.tsv").exists())
            receipt = self.export_receipt(out_dir)
            self.assertIn("\texport-failure\tfetch-fixed-start-sources\tRuntimeError\tnone\n", receipt)
            self.assertNotIn("uid=7", receipt)
        self.assertEqual(1, client.uid_calls.count(("search", None, "ALL")))
        self.assertEqual(2, client.uid_calls.count(("fetch", "7", FULL_FETCH)))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_export_fetch_timeout_leaves_no_manifest_and_does_not_retry_or_mutate(self) -> None:
        raw = self.raw_message("[worker:0] complete")

        class BlockingSocket:
            def __init__(self) -> None:
                self.closed = threading.Event()

            def shutdown(self, _how: int) -> None:
                self.closed.set()

            def close(self) -> None:
                self.closed.set()

        class BlockingFetchClient(FakeClient):
            def __init__(self) -> None:
                super().__init__(
                    {
                        ("search", None, "ALL"): ("OK", [b"7"]),
                        ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                        ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                    },
                    ManagerMailCompressTests.gmail_mailboxes(),
                )
                self.sock = BlockingSocket()
                self.release = threading.Event()

            def uid(self, *args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if args == ("fetch", "7", FULL_FETCH):
                    self.uid_calls.append(args)
                    self.release.wait()
                    raise OSError("stub socket aborted")
                return super().uid(*args)

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        client = BlockingFetchClient()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
            patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            patch("omo_manager.omo_manager_mail_compress.IMAP_OPERATION_TIMEOUT_S", 0.01),
        ):
            out_dir = Path(tmp) / "export"
            with self.assertRaisesRegex(RuntimeError, r"timed out: stage=message-fetch uid=7 timeout_s=0.01"):
                cmd_export(ExportArgs(out_dir))
            client.release.set()
            self.assertFalse((out_dir / "manifest.tsv").exists())
            receipt = self.export_receipt(out_dir)
            self.assertIn("\timap-timeout\tmessage-fetch uid=<redacted>\tdeadline-expired\tnone\n", receipt)
            self.assertNotIn("uid=7", receipt)
            self.assertEqual(1, client.uid_calls.count(("fetch", "7", FULL_FETCH)))
            self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_export_terminal_receipt_blocks_discovery_retry_after_partial_unexpected_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "export"

            def partial(_args: ExportArgs, set_stage: object) -> int:
                del set_stage
                ensure_empty_private_dir(out_dir)
                write_private(out_dir / "partial.txt", "private proof must not escape\n")
                raise ValueError("message body credential secret@example.test")

            with patch("omo_manager.omo_manager_mail_compress.run_export", side_effect=partial):
                with self.assertRaisesRegex(ValueError, "message body"):
                    cmd_export(ExportArgs(out_dir))
            receipt = self.export_receipt(out_dir)
            self.assertIn("\tunexpected-exception\tstart\tValueError\tnone\n", receipt)
            self.assertNotIn("private proof", receipt)
            self.assertNotIn("secret@example.test", receipt)
            self.assertFalse((out_dir / "manifest.tsv").exists())
            with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
                with self.assertRaisesRegex(RuntimeError, "terminal export receipt already exists"):
                    cmd_export(ExportArgs(out_dir))
            open_mailbox_mock.assert_not_called()

    def test_export_terminal_receipt_does_not_authorize_empty_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "export"
            with patch("omo_manager.omo_manager_mail_compress.run_export", side_effect=ValueError("secret body")):
                with self.assertRaisesRegex(ValueError, "secret body"):
                    cmd_export(ExportArgs(out_dir))
            self.assertFalse(out_dir.exists())
            self.assertFalse((out_dir / "manifest.tsv").exists())
            receipt = self.export_receipt(out_dir)
            self.assertIn("\tunexpected-exception\tstart\tValueError\tnone\n", receipt)
            self.assertNotIn("secret body", receipt)

    def test_export_terminal_receipt_is_private_and_fsyncs_file_and_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "export"
            fsync = os.fsync
            fsynced: list[Path] = []

            def observe(fd: int) -> None:
                fsynced.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
                fsync(fd)

            with (
                patch("omo_manager.omo_manager_mail_compress.run_export", return_value=0),
                patch("omo_manager.omo_manager_mail_compress.os.fsync", side_effect=observe),
            ):
                self.assertEqual(0, cmd_export(ExportArgs(out_dir)))
            receipt_path = export_receipt_path(out_dir)
            self.assertEqual(0o600, receipt_path.stat().st_mode & 0o777)
            self.assertEqual([receipt_path, receipt_path.parent], fsynced)

    def test_export_parent_fsync_failure_replaces_success_with_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "export"
            fsync = os.fsync
            n_calls = 0

            def fail_first_parent(fd: int) -> None:
                nonlocal n_calls
                n_calls += 1
                if n_calls == 2:
                    raise OSError("parent fsync failed")
                fsync(fd)

            with (
                patch("omo_manager.omo_manager_mail_compress.run_export", return_value=0),
                patch("omo_manager.omo_manager_mail_compress.os.fsync", side_effect=fail_first_parent),
            ):
                with self.assertRaisesRegex(OSError, "parent fsync failed"):
                    cmd_export(ExportArgs(out_dir))
            receipt = self.export_receipt(out_dir)
            self.assertIn("\treceipt-failure\tterminal-receipt\tOSError\tnone\n", receipt)
            self.assertNotIn("\tsuccess\tcomplete\t", receipt)

    def test_export_file_fsync_failure_replaces_success_with_failure_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "export"
            fsync = os.fsync
            n_calls = 0

            def fail_first_file(fd: int) -> None:
                nonlocal n_calls
                n_calls += 1
                if n_calls == 1:
                    raise OSError("file fsync failed")
                fsync(fd)

            with (
                patch("omo_manager.omo_manager_mail_compress.run_export", return_value=0),
                patch("omo_manager.omo_manager_mail_compress.os.fsync", side_effect=fail_first_file),
            ):
                with self.assertRaisesRegex(OSError, "file fsync failed"):
                    cmd_export(ExportArgs(out_dir))
            receipt = self.export_receipt(out_dir)
            self.assertIn("\treceipt-failure\tterminal-receipt\tOSError\tnone\n", receipt)
            self.assertNotIn("\tsuccess\tcomplete\t", receipt)

    def test_export_receipt_creation_race_preserves_existing_terminal_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "export"
            write_export_receipt(out_dir, "success", "complete", "none")
            original = self.export_receipt(out_dir)
            with self.assertRaisesRegex(RuntimeError, "private evidence already exists"):
                write_export_receipt(out_dir, "export-failure", "start", "RuntimeError")
            self.assertEqual(original, self.export_receipt(out_dir))

    def test_export_symlink_alias_cannot_bypass_terminal_retry_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            out_dir = parent / "actual-run"
            out_dir.mkdir()
            alias = parent / "alias"
            alias.symlink_to(out_dir, target_is_directory=True)
            with patch("omo_manager.omo_manager_mail_compress.run_export", return_value=0):
                self.assertEqual(0, cmd_export(ExportArgs(alias)))
            self.assertEqual(export_receipt_path(out_dir), export_receipt_path(alias))
            with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
                with self.assertRaisesRegex(RuntimeError, "terminal export receipt already exists"):
                    cmd_export(ExportArgs(out_dir))
            open_mailbox_mock.assert_not_called()

    def test_export_filters_excluded_subject_when_optional_import_fallback_is_active(self) -> None:
        raw = self.raw_message("PB news")
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        def open_readonly(readonly: bool) -> tuple[FakeClient, dict[str, str]]:
            self.assertTrue(readonly)
            return client, {"user": "human@example.test"}

        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=open_readonly),
            patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            patch("omo_manager.email_idle_watcher.subject_base", None),
            redirect_stdout(output),
        ):
            out_dir = Path(tmp) / "export"
            self.assertEqual(0, cmd_export(ExportArgs(out_dir)))
            self.assertIn("\tsuccess\tcomplete\tnone\tnone\n", self.export_receipt(out_dir))
            self.assertIn("\tauthority\n", self.export_receipt(out_dir))
        self.assertIn("exported=0 skipped_boundary_mismatch=1", output.getvalue())
        self.assertTrue(all(readonly for _mailbox, readonly in client.select_calls))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_export_batches_bounds_threads_without_splitting_one(self) -> None:
        records = [
            MailRecord("1", "", "", "", "one", "", gmail_msgid="101", gmail_thrid="201"),
            MailRecord("2", "", "", "", "two", "", gmail_msgid="102", gmail_thrid="202"),
            MailRecord("3", "", "", "", "three", "", gmail_msgid="103", gmail_thrid="203"),
        ]
        batches = export_batches(records, 2)

        self.assertIn("batch-0001\t201\t1", batches)
        self.assertIn("batch-0001\t202\t2", batches)
        self.assertIn("batch-0002\t203\t3", batches)

    def test_batch_claim_refuses_a_second_owner(self) -> None:
        record = MailRecord("7", "date", "from", "to", "subject", "msgid", gmail_msgid="100", gmail_thrid="200")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)

            with self.assertRaises(RuntimeError):
                claim_batch(source_dir, "batch-0001", "reviewer-2")

    def test_retain_and_final_verify_cover_only_fixed_start_sources(self) -> None:
        record = MailRecord("7", "date", "from", "to", "subject", "msgid", gmail_msgid="100", gmail_thrid="200")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)

            self.assertEqual(0, cmd_retain_thread(RetainArgs(source_dir)))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, cmd_verify_run(VerifyArgs(source_dir)))

        self.assertIn("fixed_start_verified=1 sources=1", output.getvalue())
        self.assertIn("later_arrivals_included=0 live_full_scan=0", output.getvalue())

    def test_retain_can_safely_close_a_failed_trash_intent(self) -> None:
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
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)
            prepare_thread_disposition(
                source_dir,
                "batch-0001",
                "reviewer-1",
                "200",
                {"7"},
                source_dir / "reason.txt",
                source_dir / "task-evidence.txt",
                "not-required",
            )
            with patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})):
                self.assertEqual(0, cmd_retain_thread(RetainArgs(source_dir)))
            self.assertEqual(0, cmd_verify_run(VerifyArgs(source_dir)))

        self.assertTrue(client.logged_out)

    def test_reconcile_interrupted_intent_in_inbox_without_mutation(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b"7"]), ("OK", [b""]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "7", GMAIL_METADATA_FETCH): [self.gmail_metadata("7"), self.gmail_metadata("7")],
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", set(), source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required-retained")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                self.assertEqual(0, cmd_reconcile_intent(ReconcileArgs(source_dir)))
            self.assertTrue((source_dir / "outcomes" / "200.tsv").exists())
        self.assertTrue(all(readonly for _mailbox, readonly in client.select_calls))
        self.assertEqual(('"[Gmail]/All Mail"', True), client.select_calls[-1])
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_reconcile_intent_revalidates_uidvalidity_and_all_mail_identity(self) -> None:
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", "msg", "body\n", "100", "200", "", r"\Inbox", "raw")
        cases = (
            FakeClient({}, self.gmail_mailboxes(), uidvalidity="10"),
            FakeClient({}, [b'(\\HasNoChildren \\All) "/" "Other All"', *self.gmail_mailboxes()[1:]]),
        )
        for client in cases:
            with tempfile.TemporaryDirectory() as tmp:
                source_dir = Path(tmp) / "export"
                self.write_source_map(source_dir, record)
                prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", set(), source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required-retained")
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                ):
                    with self.assertRaises(RuntimeError):
                        cmd_reconcile_intent(ReconcileArgs(source_dir))
                self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())

    def test_reconcile_intent_uses_one_in_memory_intent_snapshot(self) -> None:
        record = MailRecord("7", "date", "from", "to", "subject", "msg", gmail_msgid="100", gmail_thrid="200")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", set(), source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required-retained")
            intent_path = source_dir / "intents" / "200.tsv"
            original = intent_path.read_text(encoding="utf-8")
            real_read_text = Path.read_text
            intent_reads = 0

            def racing_read_text(path: Path, *args: object, **kwargs: object) -> str:
                nonlocal intent_reads
                text = real_read_text(path, *args, **kwargs)
                if path == intent_path:
                    intent_reads += 1
                    intent_path.write_text("changed after snapshot\n", encoding="utf-8")
                return text

            with patch.object(Path, "read_text", racing_read_text):
                evidence, rows, _source_map = intent_reconciliation_evidence(source_dir, "200")
            self.assertEqual(1, intent_reads)
            self.assertEqual(original, evidence)
            self.assertEqual(["7"], [row["uid"] for row in rows])

    def test_reconcile_interrupted_intent_in_trash_without_mutation(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                ("search", None, "X-GM-THRID", "200"): ("OK", [b""]),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                self.assertEqual(0, cmd_reconcile_intent(ReconcileArgs(source_dir)))
        self.assertTrue(all(readonly for _mailbox, readonly in client.select_calls))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_reconcile_intent_fails_closed_on_both_locations_or_content_mismatch(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        changed = self.raw_message("[worker:0] complete", "changed")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        for inbox_result, trash_raw, trash_labels in ((b"7", raw, r"\Trash"), (b"", changed, r"\Trash"), (b"", raw, r"\Trash changed-label")):
            client = FakeClient(
                {
                    ("search", None, "X-GM-MSGID", "100"): [("OK", [inbox_result]), ("OK", [b"70"])],
                    ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                    ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                    ("fetch", "70", FULL_FETCH): ("OK", [(b"message", trash_raw)]),
                    ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70", labels=trash_labels),
                },
                self.gmail_mailboxes(),
            )
            with tempfile.TemporaryDirectory() as tmp:
                source_dir = Path(tmp) / "export"
                self.write_source_map(source_dir, record)
                prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                ):
                    with self.assertRaises(RuntimeError):
                        cmd_reconcile_intent(ReconcileArgs(source_dir))
                self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
            self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_reconcile_ignores_unrelated_label_drift(self) -> None:
        record = MailRecord("70", "", "", "", "", "msg", gmail_msgid="100", gmail_thrid="200", labels=r"\Trash Project Alpha", raw_sha256="raw")
        source = {
            "gmail_msgid": "100",
            "gmail_thrid": "200",
            "msgid_sha256": "msg",
            "raw_sha256": "raw",
            "flags": "",
            "labels": r'\Inbox "Project Alpha"',
        }
        self.assertTrue(record_matches_reconciliation_location(record, source, "Trash"))

    def test_gmail_signal_drift_is_ignored_by_identity_match(self) -> None:
        source = {
            "gmail_msgid": "100",
            "gmail_thrid": "200",
            "msgid_sha256": "msg",
            "raw_sha256": "raw",
            "flags": r"\Flagged",
            "labels": r'\Inbox \Important \Starred "Read Later" Saved Security',
        }
        exact = MailRecord("70", "", "", "", "", "msg", gmail_msgid="100", gmail_thrid="200", flags=r"\Flagged", labels=r'\Trash \Important \Starred "Read Later" Saved Security', raw_sha256="raw")
        changed_flag = MailRecord("70", "", "", "", "", "msg", gmail_msgid="100", gmail_thrid="200", flags="", labels=exact.labels, raw_sha256="raw")

        self.assertTrue(record_matches_reconciliation_location(exact, source, "Trash"))
        self.assertTrue(record_matches_reconciliation_location(changed_flag, source, "Trash"))

    def test_post_move_verification_ignores_signal_label_drift(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        msgid_digest = hashlib.sha256(b"<one@example.test>").hexdigest()[:12]
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70", flags=r"\Flagged", labels=r"\Trash \Important"),
            }
        )
        source_map = {
            "7": {
                "gmail_msgid": "100",
                "gmail_thrid": "200",
                "msgid_sha256": msgid_digest,
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "flags": r"\Flagged",
                "labels": r"\Inbox \Important \Starred",
            }
        }

        result = verify_post_move_imap(client, source_map, "agent@example.test", "human@example.test")

        self.assertTrue(result.complete)
        self.assertEqual(0, result.changed_thread_count)

    def test_reconcile_intent_rejects_changed_non_source_thread_member(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        context_raw = self.raw_message("[worker:0] context", "context").replace(b"<one@example.test>", b"<two@example.test>")
        changed_context_raw = context_raw.replace(b"context\r\n", b"changed context\r\n")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        context = MailRecord("71", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] context", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "context\n", "101", "200", "", r"\Inbox", hashlib.sha256(context_raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70", labels=r"\Trash"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"71"]),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", changed_context_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record, [record, context])
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                with self.assertRaises(RuntimeError):
                    cmd_reconcile_intent(ReconcileArgs(source_dir))
            self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_reconcile_intent_allows_additive_later_context(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        later_raw = self.raw_message("[worker:0] later", "later").replace(b"<one@example.test>", b"<later@example.test>")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash changed-signal")],
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"71"]),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", later_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, source)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                self.assertEqual(0, cmd_reconcile_intent(ReconcileArgs(source_dir)))
            self.assertTrue((source_dir / "outcomes" / "200.tsv").exists())
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_recover_already_trashed_allows_only_additive_later_context_and_verifies_run(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        later_raw = self.raw_message("[worker:0] later", "later").replace(b"<one@example.test>", b"<later@example.test>")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"71"]),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", later_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, source)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                self.assertEqual(0, cmd_recover_already_trashed(ReconcileArgs(source_dir)))
            self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
            self.assertTrue((source_dir / "recoveries" / "200.skipped-already-trashed.tsv").exists())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, cmd_verify_run(VerifyArgs(source_dir)))
            self.assertIn("trashed=0 skipped_already_trashed=1 skipped_already_trashed_threads=1", output.getvalue())
        self.assertTrue(all(readonly for _mailbox, readonly in client.select_calls))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_recover_already_trashed_rejects_changed_frozen_context(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        context_raw = self.raw_message("[worker:0] context", "context").replace(b"<one@example.test>", b"<two@example.test>")
        changed = context_raw.replace(b"context\r\n", b"changed\r\n")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        context = MailRecord("71", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] context", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "context\n", "101", "200", "", r"\Inbox", hashlib.sha256(context_raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"71"]),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", changed)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, source, [source, context])
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                with self.assertRaises(RuntimeError):
                    cmd_recover_already_trashed(ReconcileArgs(source_dir))
            self.assertFalse((source_dir / "recoveries" / "200.skipped-already-trashed.tsv").exists())

    def test_recover_already_trashed_requires_additive_later_context(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                ("search", None, "X-GM-THRID", "200"): ("OK", [b""]),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, source)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            ):
                with self.assertRaises(RuntimeError):
                    cmd_recover_already_trashed(ReconcileArgs(source_dir))
            self.assertFalse((source_dir / "recoveries" / "200.skipped-already-trashed.tsv").exists())

    def test_recover_already_trashed_rejects_shortened_frozen_context_snapshot(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        context_raw = self.raw_message("[worker:0] context", "context").replace(b"<one@example.test>", b"<two@example.test>")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        context = MailRecord("71", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] context", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "context\n", "101", "200", "", r"\Inbox", hashlib.sha256(context_raw).hexdigest())
        for removed_msgid in ("100", "101"):
            client = FakeClient(
                {
                    ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                    ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                    ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                },
                self.gmail_mailboxes(),
            )
            with tempfile.TemporaryDirectory() as tmp:
                source_dir = Path(tmp) / "export"
                self.write_source_map(source_dir, source, [source, context])
                context_path = source_dir / "thread-context.tsv"
                lines = context_path.read_text(encoding="utf-8").splitlines()
                context_path.write_text("\n".join([lines[0], *(line for line in lines[1:] if line.split("\t")[1] != removed_msgid)]) + "\n", encoding="utf-8")
                prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                ):
                    with self.assertRaises(RuntimeError):
                        cmd_recover_already_trashed(ReconcileArgs(source_dir))
                self.assertFalse((source_dir / "recoveries" / "200.skipped-already-trashed.tsv").exists())

    def test_recover_already_trashed_rejects_ambiguous_or_wrong_location_without_mutation(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        for inbox, trash in ((b"7", b"70"), (b"", b"70 72")):
            client = FakeClient({("search", None, "X-GM-MSGID", "100"): [("OK", [inbox]), ("OK", [trash])]}, self.gmail_mailboxes())
            with tempfile.TemporaryDirectory() as tmp:
                source_dir = Path(tmp) / "export"
                self.write_source_map(source_dir, source)
                prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                ):
                    with self.assertRaises(RuntimeError):
                        cmd_recover_already_trashed(ReconcileArgs(source_dir))
                self.assertFalse((source_dir / "recoveries" / "200.skipped-already-trashed.tsv").exists())
            self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_recover_already_trashed_rechecks_location_after_context_gate(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        source = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
            },
            self.gmail_mailboxes(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, source)
            prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", {"7"}, source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                patch("omo_manager.omo_manager_mail_compress.additive_recovery_thread_intact", return_value=True),
            ):
                with self.assertRaises(RuntimeError):
                    cmd_recover_already_trashed(ReconcileArgs(source_dir))
            self.assertFalse((source_dir / "recoveries" / "200.skipped-already-trashed.tsv").exists())
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_final_verify_refuses_unclassified_fixed_source(self) -> None:
        record = MailRecord("7", "date", "from", "to", "subject", "msgid", gmail_msgid="100", gmail_thrid="200")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)

            with self.assertRaises(RuntimeError):
                cmd_verify_run(VerifyArgs(source_dir))

    def test_final_verify_aggregates_every_missing_terminal_result(self) -> None:
        first = MailRecord("7", "date", "from", "to", "first", "msg1", gmail_msgid="100", gmail_thrid="200")
        second = MailRecord("8", "date", "from", "to", "second", "msg2", gmail_msgid="101", gmail_thrid="201")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, first)
            digest = thread_context_digest([first])
            (source_dir / "manifest.tsv").write_text(
                "uid\tsource_mailbox\tuidvalidity\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tbody_bytes\tsubject\n"
                f"7\tINBOX\t9\tdate\t100\t200\tmsg1\t\t\t\\Inbox\t{digest}\t0\tfirst\n"
                f"8\tINBOX\t9\tdate\t101\t201\tmsg2\t\t\t\\Inbox\t{digest}\t0\tsecond\n",
                encoding="utf-8",
            )
            (source_dir / "batches.tsv").write_text(export_batches([first, second], 10), encoding="utf-8")
            (source_dir / "run.tsv").write_text("fixed_start_utc\tsource_count\tthread_count\tthreads_per_batch\n2026-08-11T00:00:00+00:00\t2\t2\t10\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, r"count=2 threads=200,201"):
                cmd_verify_run(VerifyArgs(source_dir))

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

    def test_replacement_requires_one_exact_message_from_agent_to_human(self) -> None:
        replacement = b"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nMessage-ID: <replacement@example.test>\r\n\r\nsummary\r\n"
        wrong_recipient = replacement.replace(b"human@example.test", b"other@example.test")
        wrong_sender = replacement.replace(b"agent@example.test", b"other@example.test")
        cases = (
            ("unique", ("OK", [b"90"]), replacement, True),
            ("missing", ("OK", [b""]), replacement, False),
            ("ambiguous", ("OK", [b"90 91"]), replacement, False),
            ("wrong-recipient", ("OK", [b"90"]), wrong_recipient, False),
            ("wrong-sender", ("OK", [b"90"]), wrong_sender, False),
        )
        for name, search_result, raw, expected in cases:
            with self.subTest(name=name):
                client = FakeClient(
                    {
                        ("search", None, "HEADER", "Message-ID", '"<replacement@example.test>"'): search_result,
                        ("fetch", "90", HEADER_FETCH): ("OK", [(b"header", raw)]),
                    }
                )
                self.assertEqual(
                    expected,
                    replacement_exists(
                        client,
                        "[Gmail]/All Mail",
                        "<replacement@example.test>",
                        "agent@example.test",
                        "human@example.test",
                    ),
                )

    def test_trash_superseded_requires_yes(self) -> None:
        self.assertEqual(2, cmd_trash_superseded(Args(uids="7")))

    def test_mark_seen_is_retired_for_compression(self) -> None:
        self.assertEqual(2, cmd_mark_seen(Args(uids="7", yes=True)))

    def test_trash_superseded_requires_private_source_map(self) -> None:
        self.assertEqual(2, cmd_trash_superseded(Args(uids="7", yes=True)))

    def test_trash_help_states_uid_file_is_required(self) -> None:
        helper = Path(__file__).parents[1] / "omo_manager_mail_compress.py"
        result = subprocess.run([helper, "trash-superseded", "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("inline UIDs are refused", result.stdout)
        self.assertIn("--uid-file is authoritative", result.stdout)

    def test_trash_superseded_refuses_inline_uid_broadening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uid_file = Path(tmp) / "uids.txt"
            uid_file.write_text("7\n", encoding="utf-8")
            args = Args(uids="8", uid_file=uid_file, yes=True)
            with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
                self.assertEqual(2, cmd_trash_superseded(args))
            open_mailbox_mock.assert_not_called()

    def test_trash_superseded_blocks_before_move_when_replacement_is_missing(self) -> None:
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
            {("search", None, "HEADER", "Message-ID", '"<replacement@example.test>"'): ("OK", [b""])},
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            uid_file = self.write_source_map(Path(tmp) / "export", record)
            args = Args(uid_file=uid_file, yes=True)
            args.replacement_id = "<replacement@example.test>"
            args.replacement_not_required = False
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(args))

        self.assertFalse(any(call[0] == "MOVE" for call in client.uid_calls))

    def test_trash_superseded_verifies_split_account_replacement_in_recipient_all_mail(self) -> None:
        source_raw = self.raw_message("[worker:0] complete")
        source = MailRecord(
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
            hashlib.sha256(source_raw).hexdigest(),
        )
        replacement = b"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nMessage-ID: <replacement@example.test>\r\n\r\nsummary\r\n"

        class RecipientMailboxClient(FakeClient):
            current_mailbox = "INBOX"

            def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
                self.current_mailbox = mailbox
                return super().select(mailbox, readonly)

            def uid(self, *args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if args == ("search", None, "HEADER", "Message-ID", '"<replacement@example.test>"') and self.current_mailbox != '"[Gmail]/All Mail"':
                    self.uid_calls.append(args)
                    return "OK", [b""]
                return super().uid(*args)

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        cases = (
            ("exact", replacement, True),
            ("wrong-sender", replacement.replace(b"agent@example.test", b"other@example.test"), False),
            ("wrong-recipient", replacement.replace(b"human@example.test", b"other@example.test"), False),
        )
        for name, replacement_raw, expected_move in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                client = RecipientMailboxClient(
                    {
                        ("search", None, "HEADER", "Message-ID", '"<replacement@example.test>"'): ("OK", [b"90"]),
                        ("fetch", "90", HEADER_FETCH): ("OK", [(b"header", replacement_raw)]),
                        ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                        ("fetch", "7", FULL_FETCH): ("OK", [(b"message", source_raw)]),
                        ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                        ("search", None, "X-GM-THRID", "200"): [("OK", [b"70"]), ("OK", [b"70"]), ("OK", [b""])],
                        ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                        ("fetch", "70", FULL_FETCH): ("OK", [(b"message", source_raw)]),
                        ("fetch", "70", GMAIL_METADATA_FETCH): [
                            self.gmail_metadata("70"),
                            self.gmail_metadata("70"),
                            self.gmail_metadata("70", labels=r"\Trash"),
                        ],
                        ("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""]),
                    },
                    self.gmail_mailboxes(),
                )
                uid_file = self.write_source_map(Path(tmp) / "export", source)
                args = Args(uid_file=uid_file, yes=True)
                args.replacement_id = "<replacement@example.test>"
                args.replacement_not_required = False
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                    patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
                ):
                    self.assertEqual(0 if expected_move else 1, cmd_trash_superseded(args))
                self.assertEqual(expected_move, any(call[0] == "MOVE" for call in client.uid_calls))
                self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)
                self.assertNotIn(('"[Gmail]/Sent Mail"', True), client.select_calls)

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
                ("search", None, "X-GM-THRID", "200"): [("OK", [b"70"]), ("OK", [b"70"]), ("OK", [b""])],
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): [
                    self.gmail_metadata("70"),
                    self.gmail_metadata("70"),
                    self.gmail_metadata("70", labels=r"\Trash"),
                ],
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

    def test_trash_superseded_ignores_all_gmail_signals_for_eligibility(self) -> None:
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
            r'\Inbox \Important \Starred "Read Later" Saved Security',
            hashlib.sha256(raw).hexdigest(),
        )
        client = FakeClient(
            {
                ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7", labels=r"\Inbox ChangedBeforeMove"),
                ("search", None, "X-GM-THRID", "200"): [("OK", [b"70"]), ("OK", [b"70"]), ("OK", [b""])],
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): [
                    self.gmail_metadata("70", labels=r"\Inbox ChangedBeforeMove"),
                    self.gmail_metadata("70", flags=r"\Seen", labels=r"\Inbox ChangedAgain"),
                    self.gmail_metadata("70", flags=r"\Seen", labels=r"\Trash ChangedDuringMove"),
                ],
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

    def test_trash_superseded_moves_one_intermediate_and_retains_other_fixed_source(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        second_raw = self.raw_message("[worker:0] complete", "keep this").replace(b"<one@example.test>", b"<two@example.test>")
        first = MailRecord(
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
        second = MailRecord(
            "8",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            hashlib.sha256(b"<two@example.test>").hexdigest()[:12],
            "keep this\n",
            "101",
            "200",
            "",
            r"\Inbox",
            hashlib.sha256(second_raw).hexdigest(),
        )
        digest = thread_context_digest([first, second])
        client = FakeClient(
            {
                ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): [("OK", [b"70 71"]), ("OK", [b"70 71"]), ("OK", [b"71"])],
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): [
                    self.gmail_metadata("70"),
                    self.gmail_metadata("70"),
                    self.gmail_metadata("70", labels=r"\Trash"),
                ],
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", second_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""]),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, first, [first, second])
            (source_dir / "manifest.tsv").write_text(
                "uid\tsource_mailbox\tuidvalidity\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tbody_bytes\tsubject\n"
                f"7\tINBOX\t9\tdate\t100\t200\t{first.msgid_sha256}\t{first.raw_sha256}\t\t\\Inbox\t{digest}\t{first.body_bytes}\t{first.subject}\n"
                f"8\tINBOX\t9\tdate\t101\t200\t{second.msgid_sha256}\t{second.raw_sha256}\t\t\\Inbox\t{digest}\t{second.body_bytes}\t{second.subject}\n",
                encoding="utf-8",
            )
            (source_dir / "batches.tsv").write_text(export_batches([first, second], 10), encoding="utf-8")
            (source_dir / "run.tsv").write_text(
                "fixed_start_utc\tsource_count\tthread_count\tthreads_per_batch\n2026-08-09T00:00:00+00:00\t2\t1\t10\n",
                encoding="utf-8",
            )
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(0, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
            self.assertEqual(0, cmd_verify_run(VerifyArgs(source_dir)))
            outcome = (source_dir / "outcomes" / "200.tsv").read_text(encoding="utf-8")

        self.assertIn("\t7\ttrashed\t", outcome)
        self.assertIn("\t8\tretained\t", outcome)
        self.assertIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_skips_verified_trash_source_and_moves_only_inbox_remainder(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        second_raw = self.raw_message("[worker:0] complete", "second").replace(b"<one@example.test>", b"<two@example.test>")
        first = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        second = MailRecord("8", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "second\n", "101", "200", "", r"\Inbox", hashlib.sha256(second_raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "UID", "7,8"): [("OK", [b"8"]), ("OK", [b"8"]), ("OK", [b"8"]), ("OK", [b""])],
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b"70"]), ("OK", [b"70"]), ("OK", [b"70"])],
                ("search", None, "X-GM-MSGID", "101"): ("OK", [b"71"]),
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
                ("fetch", "8", FULL_FETCH): [("OK", [(b"message", second_raw)]), ("OK", [(b"message", second_raw)])],
                ("fetch", "8", GMAIL_METADATA_FETCH): [self.gmail_metadata("8", gmail_msgid="101"), self.gmail_metadata("8", gmail_msgid="101")],
                ("search", None, "X-GM-THRID", "200"): [("OK", [b"71"]), ("OK", [b"71"]), ("OK", [b""])],
                ("fetch", "71", FULL_FETCH): [("OK", [(b"message", second_raw)]), ("OK", [(b"message", second_raw)]), ("OK", [(b"message", second_raw)])],
                ("fetch", "71", GMAIL_METADATA_FETCH): [
                    self.gmail_metadata("71", gmail_msgid="101"),
                    self.gmail_metadata("71", gmail_msgid="101"),
                    self.gmail_metadata("71", gmail_msgid="101", labels=r"\Trash"),
                ],
                ("MOVE", "8", '"[Gmail]/Trash"'): ("OK", [b""]),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, first, [first, second])
            (source_dir / "manifest.tsv").write_text(
                "uid\tsource_mailbox\tuidvalidity\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tbody_bytes\tsubject\n"
                f"7\tINBOX\t9\tdate\t100\t200\t{first.msgid_sha256}\t{first.raw_sha256}\t\t\\Inbox\t{thread_context_digest([first, second])}\t{first.body_bytes}\t{first.subject}\n"
                f"8\tINBOX\t9\tdate\t101\t200\t{second.msgid_sha256}\t{second.raw_sha256}\t\t\\Inbox\t{thread_context_digest([first, second])}\t{second.body_bytes}\t{second.subject}\n",
                encoding="utf-8",
            )
            (source_dir / "batches.tsv").write_text(export_batches([first, second], 10), encoding="utf-8")
            uid_file.write_text("7\n8\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(0, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
        self.assertIn(("MOVE", "8", '"[Gmail]/Trash"'), client.uid_calls)
        self.assertNotIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_rechecks_existing_trash_source_before_move(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        second_raw = self.raw_message("[worker:0] complete", "second").replace(b"<one@example.test>", b"<two@example.test>")
        first = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        second = MailRecord("8", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "second\n", "101", "200", "", r"\Inbox", hashlib.sha256(second_raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "UID", "7,8"): [("OK", [b"8"]), ("OK", [b"8"])],
                ("fetch", "8", FULL_FETCH): [("OK", [(b"message", second_raw)]), ("OK", [(b"message", second_raw)])],
                ("fetch", "8", GMAIL_METADATA_FETCH): [self.gmail_metadata("8", gmail_msgid="101"), self.gmail_metadata("8", gmail_msgid="101")],
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, first, [first, second])
            digest = thread_context_digest([first, second])
            (source_dir / "manifest.tsv").write_text(
                "uid\tsource_mailbox\tuidvalidity\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tbody_bytes\tsubject\n"
                f"7\tINBOX\t9\tdate\t100\t200\t{first.msgid_sha256}\t{first.raw_sha256}\t\t\\Inbox\t{digest}\t{first.body_bytes}\t{first.subject}\n"
                f"8\tINBOX\t9\tdate\t101\t200\t{second.msgid_sha256}\t{second.raw_sha256}\t\t\\Inbox\t{digest}\t{second.body_bytes}\t{second.subject}\n",
                encoding="utf-8",
            )
            (source_dir / "batches.tsv").write_text(export_batches([first, second], 10), encoding="utf-8")
            uid_file.write_text("7\n8\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
                patch("omo_manager.omo_manager_mail_compress.verified_existing_trash_records", side_effect=([first], RuntimeError("Trash source changed before move"))),
                patch("omo_manager.omo_manager_mail_compress.reconciliation_thread_unchanged", return_value=True),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
            self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
        self.assertFalse(any(call[0].casefold() == "move" for call in client.uid_calls))

    def test_trash_superseded_all_already_trashed_still_rejects_changed_thread(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        context_raw = self.raw_message("[worker:0] context", "context").replace(b"<one@example.test>", b"<two@example.test>")
        changed_context = context_raw.replace(b"context\r\n", b"changed\r\n")
        first = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        context = MailRecord("71", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] context", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "context\n", "101", "200", "", r"\Inbox", hashlib.sha256(context_raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b""]),
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70", labels=r"\Trash"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"71"]),
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", changed_context)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, first, [first, context])
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(1, cmd_trash_superseded(Args(uid_file=uid_file, yes=True)))
            self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
        self.assertFalse(any(call[0].casefold() == "move" for call in client.uid_calls))

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

    def test_trash_superseded_allows_additive_later_context_without_moving_it(self) -> None:
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
        client = FakeClient(
            {
                ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): [("OK", [b"70 71"]), ("OK", [b"70 71"]), ("OK", [b"71"])],
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70"), self.gmail_metadata("70"), self.gmail_metadata("70", labels=r"\Trash")],
                ("fetch", "71", FULL_FETCH): ("OK", [(b"message", other_raw)]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="101"),
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
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
        self.assertFalse(any(call[0] == "MOVE" and call[1] == "71" for call in client.uid_calls))

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
