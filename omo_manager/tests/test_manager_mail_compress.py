from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager.omo_manager_mail_compress import (
    FULL_FETCH,
    FULL_BATCH_FETCH,
    FINAL_GATE_FETCH,
    GMAIL_METADATA_FETCH,
    GMAIL_METADATA_BATCH_FETCH,
    HEADER_BATCH_FETCH,
    HEADER_FETCH,
    ExactRemovalEvidence,
    ImapOperationError,
    MailRecord,
    SOURCE_815_APPROVAL_FILE,
    SOURCE_815_APPROVAL_QUOTE,
    SOURCE_815_APPROVAL_QUOTE_SHA256,
    SOURCE_815_EXACT_REMOVAL_EXCEPTION,
    SOURCE_815_TASK_ID,
    SOURCE_1140_APPROVAL_FILE,
    SOURCE_1140_APPROVAL_QUOTE,
    accepted_manager_headers,
    agent_unread_records,
    claim_batch,
    cmd_reconcile_intent,
    cmd_recover_already_trashed,
    cmd_retain_thread,
    cmd_export,
    cmd_identity_preflight,
    cmd_inspect_explicit,
    cmd_snapshot,
    cmd_locate_replacement,
    cmd_mark_seen,
    cmd_unread_summary,
    cmd_agent_trash_replaced,
    current_agent_mail_target,
    current_agent_session_id,
    cmd_trash_explicit,
    cmd_trash_superseded,
    direct_context_intact,
    direct_contexts_intact,
    cmd_verify_run,
    connect_mailbox,
    ensure_empty_private_dir,
    export_receipt_path,
    final_inbox_bindings_intact,
    export_failure_diagnostics,
    export_body,
    export_batches,
    fetch_msg_bytes,
    fetch_gmail_metadata,
    fetch_header_records,
    fetch_direct_thread_contexts,
    fetch_full_records,
    fetch_final_gate_records,
    fetch_imap_thread_contexts,
    discover_gmail_thread_member_uids,
    gmail_thrid_or_query,
    imap_operation,
    imap_quoted,
    intent_reconciliation_evidence,
    is_manager_record,
    load_reviewed_scope,
    mail_boundary,
    mailbox_exists,
    parse_explicit_context,
    parse_explicit_source,
    parse_retained_replacement,
    parse_route_resolutions,
    require_source_1140_direct_removal,
    manager_candidate_uids,
    manager_unread_candidate_uids,
    parse_uid_text,
    prepare_thread_disposition,
    record_from_msg,
    record_matches_reconciliation_location,
    replacement_exists,
    retained_replacements_intact,
    strict_fresh_source_locations_intact,
    special_use_mailboxes,
    frozen_thread_context,
    thread_context_digest,
    tsv_value,
    unread_records_with_metadata,
    verify_post_move_imap,
    validate_task_terminal_dispositions,
    validate_scoped_records,
    verified_existing_trash_records,
    write_export_receipt,
    write_private,
)
from omo_manager.omo_email_subject import subject_tmux_target

TEST_RETAINED_BODY = "x"
TEST_RETAINED_REPLACEMENT = f"18692:999:300:{'d' * 64}:1:{hashlib.sha256(TEST_RETAINED_BODY.encode()).hexdigest()}"
TEST_RETAINED_REPLACEMENT_998 = f"18693:998:301:{'e' * 64}:1:{hashlib.sha256(TEST_RETAINED_BODY.encode()).hexdigest()}"


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
        self.task_id = "task-a"
        self.reviewer = "reviewer-2"
        self.human_approved_exact_removal = False
        self.human_approval_file: Path | None = None
        self.human_approval_quote: str | None = None


class UnreadSummaryArgs:
    def __init__(self, max_threads: int = 20, max_body_chars: int = 1200, max_messages_per_thread: int = 20) -> None:
        self.max_threads = max_threads
        self.max_body_chars = max_body_chars
        self.max_messages_per_thread = max_messages_per_thread


class ExportArgs:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.threads_per_batch = 10
        self.scope_file = None


class RetainArgs:
    def __init__(self, source_dir: Path, gmail_thrid: str = "200") -> None:
        self.source_dir = source_dir
        self.batch_id = "batch-0001"
        self.owner = "reviewer-1"
        self.gmail_thrid = gmail_thrid
        self.reason_file = source_dir / "reason.txt"
        self.task_evidence_file = source_dir / "task-evidence.txt"
        self.task_id = "task-a"
        self.reviewer = "reviewer-2"


class VerifyArgs:
    def __init__(self, source_dir: Path) -> None:
        self.source_dir = source_dir


class ReconcileArgs:
    def __init__(self, source_dir: Path, gmail_thrid: str = "200") -> None:
        self.source_dir = source_dir
        self.gmail_thrid = gmail_thrid


class ManagerMailCompressTests(unittest.TestCase):
    def test_route_resolution_requires_one_exact_valid_binding_per_task(self) -> None:
        self.assertEqual({"task-a": "wl:31"}, parse_route_resolutions(["task-a=wl:31.0"], ["task-a"]))
        self.assertEqual({"task=a": "wl:31"}, parse_route_resolutions(["task=a=wl:31"], ["task=a"]))
        for values, tasks in (
            (["wrong=wl:31"], ["task-a"]),
            (["task-a=wl:31", "task-a=wl:32"], ["task-a"]),
            (["task-a=not-a-target"], ["task-a"]),
            (["task-a=wl:31"], ["task-a", "task-b"]),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                parse_route_resolutions(values, tasks)

    def test_subject_tmux_target_preserves_original_sender_identity(self) -> None:
        self.assertEqual("wl:7", subject_tmux_target("Re: [a] [wl:7.0] task update"))
        self.assertEqual("wl:7.2", subject_tmux_target("Re: wl:7.2 task update"))
        self.assertEqual("", subject_tmux_target("task update"))

    def test_reviewed_scope_binds_hash_review_and_rejects_tamper_or_task_mismatch(self) -> None:
        digest = hashlib.sha256(b"raw").hexdigest()
        text = (
            "version\ttask_id\tuid\tgmail_msgid\tgmail_thrid\traw_sha256\tpreparer\treviewer\tprovenance\n"
            f"v1.0.0\ttask-a\t7\t100\t200\t{digest}\towner-a\treviewer-b\tread-only current-mail inventory\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            scope_file = Path(tmp) / "scope.tsv"
            scope_file.write_text(text, encoding="utf-8")
            scope_file.chmod(0o600)
            scope = load_reviewed_scope(scope_file)
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), scope.sha256)
            validate_scoped_records(scope, [MailRecord("7", "", "", "", "", "", gmail_msgid="100", gmail_thrid="200", raw_sha256=digest)])
            with self.assertRaisesRegex(RuntimeError, "does not match reviewed scope"):
                validate_scoped_records(scope, [MailRecord("7", "", "", "", "", "", gmail_msgid="100", gmail_thrid="200", raw_sha256="0" * 64)])
            scope_file.write_text(text.replace("owner-a\treviewer-b", "owner-a\towner-a"), encoding="utf-8")
            scope_file.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "reviewer must be distinct"):
                load_reviewed_scope(scope_file)
            scope_file.write_text(text + f"v1.0.0\ttask-b\t8\t101\t200\t{digest}\towner-a\treviewer-b\tread-only current-mail inventory\n", encoding="utf-8")
            scope_file.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "multiple tasks"):
                load_reviewed_scope(scope_file)

    def test_scoped_export_excludes_unrelated_and_freezes_before_fetch(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        unrelated = self.raw_message("[worker:1] unrelated")
        digest = hashlib.sha256(raw).hexdigest()
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7 8"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"],
                ),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70"),
                ("fetch", "8", FULL_FETCH): ("OK", [(b"message", unrelated)]),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope_file = root / "scope.tsv"
            scope_file.write_text(
                "version\ttask_id\tuid\tgmail_msgid\tgmail_thrid\traw_sha256\tpreparer\treviewer\tprovenance\n"
                f"v1.0.0\ttask-a\t7\t100\t200\t{digest}\towner-a\treviewer-b\tcurrent read-only mail\n",
                encoding="utf-8",
            )
            scope_file.chmod(0o600)
            args = ExportArgs(root / "export")
            args.scope_file = scope_file
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                self.assertEqual(0, cmd_export(args))
            self.assertIn("scope_sha256", (args.out_dir / "scope.tsv").read_text(encoding="utf-8"))
            self.assertNotIn("\n8\t", (args.out_dir / "manifest.tsv").read_text(encoding="utf-8"))
            claim_batch(args.out_dir, "batch-0001", "owner-a")
            reason = args.out_dir / "reason.txt"
            task_evidence = args.out_dir / "task-evidence.txt"
            reason.write_text("reviewed reason\n", encoding="utf-8")
            task_evidence.write_text("reviewed task evidence\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not match independently reviewed scope"):
                prepare_thread_disposition(args.out_dir, "batch-0001", "owner-a", "200", set(), reason, task_evidence, "not-required-retained", "task-b", "reviewer-b")
            scope_tasks = args.out_dir / "scope-tasks.tsv"
            scope_tasks.write_text(scope_tasks.read_text(encoding="utf-8").replace("task-a", "task-b"), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "digest is missing or tampered"):
                prepare_thread_disposition(args.out_dir, "batch-0001", "owner-a", "200", set(), reason, task_evidence, "not-required-retained", "task-a", "reviewer-b")
            self.assertFalse(any(call[:2] == ("fetch", "8") for call in client.uid_calls))
            self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_scoped_export_rejects_missing_current_identity(self) -> None:
        digest = hashlib.sha256(b"raw").hexdigest()
        client = FakeClient({("search", None, "ALL"): ("OK", [b"8"]), ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"8"])})

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scope_file = root / "scope.tsv"
            scope_file.write_text(
                "version\ttask_id\tuid\tgmail_msgid\tgmail_thrid\traw_sha256\tpreparer\treviewer\tprovenance\n"
                f"v1.0.0\ttask-a\t7\t100\t200\t{digest}\towner-a\treviewer-b\tcurrent read-only mail\n",
                encoding="utf-8",
            )
            scope_file.chmod(0o600)
            args = ExportArgs(root / "export")
            args.scope_file = scope_file
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
            ):
                with self.assertRaisesRegex(RuntimeError, "absent at fixed start"):
                    cmd_export(args)
            self.assertFalse((args.out_dir / "manifest.tsv").exists())

    def test_task_terminal_rejects_multiple_retained_messages(self) -> None:
        rows = [
            {"task_id": "task-a", "uid": "7", "disposition": "retained", "replacement": "not-required-retained"},
            {"task_id": "task-a", "uid": "8", "disposition": "retained", "replacement": "not-required-retained"},
        ]
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            validate_task_terminal_dispositions(rows)

    def test_task_terminal_rejects_zero_retained_and_no_replacement(self) -> None:
        rows = [{"task_id": "task-a", "uid": "7", "disposition": "trashed", "replacement": "not-required"}]
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            validate_task_terminal_dispositions(rows)

    def test_task_terminal_accepts_one_verified_replacement_after_all_originals_trashed(self) -> None:
        rows = [
            {"task_id": "task-a", "uid": "7", "disposition": "trashed", "replacement": "<replacement@example.test>"},
            {"task_id": "task-a", "uid": "8", "disposition": "trashed", "replacement": "<replacement@example.test>"},
        ]
        validate_task_terminal_dispositions(rows)

    def test_task_terminal_blocks_replacement_with_retained_original(self) -> None:
        rows = [
            {"task_id": "task-a", "uid": "7", "disposition": "retained", "replacement": "<replacement@example.test>"},
            {"task_id": "task-a", "uid": "8", "disposition": "trashed", "replacement": "<replacement@example.test>"},
        ]
        with self.assertRaisesRegex(RuntimeError, "does not supersede every"):
            validate_task_terminal_dispositions(rows)

    def test_task_terminal_rejects_conflicting_replacement_identities(self) -> None:
        rows = [
            {"task_id": "task-a", "uid": "7", "disposition": "trashed", "replacement": "<one@example.test>"},
            {"task_id": "task-a", "uid": "8", "disposition": "trashed", "replacement": "<two@example.test>"},
        ]
        with self.assertRaisesRegex(RuntimeError, "conflicting replacement"):
            validate_task_terminal_dispositions(rows)

    def test_task_terminal_rejects_malformed_replacement_identity(self) -> None:
        rows = [{"task_id": "task-a", "uid": "7", "disposition": "trashed", "replacement": "<broken identity>"}]
        with self.assertRaisesRegex(RuntimeError, "malformed replacement"):
            validate_task_terminal_dispositions(rows)

    def test_task_terminal_rejects_mixed_replacement_and_not_required(self) -> None:
        rows = [
            {"task_id": "task-a", "uid": "7", "disposition": "trashed", "replacement": "<one@example.test>"},
            {"task_id": "task-a", "uid": "8", "disposition": "trashed", "replacement": "not-required"},
        ]
        with self.assertRaisesRegex(RuntimeError, "not bound"):
            validate_task_terminal_dispositions(rows)

    def test_trash_rejects_malformed_replacement_before_intent(self) -> None:
        record = MailRecord("7", "date", "from", "to", "subject", "msgid", gmail_msgid="100", gmail_thrid="200")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            args = Args(uid_file=uid_file, yes=True)
            args.replacement_id = "<broken identity>"
            args.replacement_not_required = False
            self.assertEqual(2, cmd_trash_superseded(args))
            self.assertFalse((source_dir / "intents" / "200.tsv").exists())

    def test_disposition_rejects_owner_as_reviewer_before_intent(self) -> None:
        record = MailRecord("7", "date", "from", "to", "subject", "msgid", gmail_msgid="100", gmail_thrid="200")
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            self.write_source_map(source_dir, record)
            with self.assertRaisesRegex(RuntimeError, "distinct"):
                prepare_thread_disposition(source_dir, "batch-0001", "reviewer-1", "200", set(), source_dir / "reason.txt", source_dir / "task-evidence.txt", "not-required-retained", "task-a", "reviewer-1")
            self.assertFalse((source_dir / "intents" / "200.tsv").exists())

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

    @staticmethod
    def write_human_approval(parent: Path, quote: str = SOURCE_815_APPROVAL_QUOTE) -> Path:
        approval_dir = parent / "manager_mail"
        approval_dir.mkdir(mode=0o700)
        approval_file = approval_dir / SOURCE_815_APPROVAL_FILE
        approval_file.write_text(
            f"{quote}\n"
            "If moved, only this exact email would be moved to recoverable Gmail Trash.\n"
            "Reply option: explicit approval to move this exact email to recoverable Gmail Trash.\n"
            "from: sichangheagent@gmail.com\n"
            "date: Thu, Aug 20, 2026, 12:36 p.m. PDT\n"
            "subject: Re: [wl:1] What happened to the rewrite of our manager orchestrating tool set\n",
            encoding="utf-8",
        )
        approval_file.chmod(0o600)
        return approval_file

    @staticmethod
    def write_local_env_root(parent: Path) -> Path:
        local_env = parent / "local.env"
        local_env.write_text(f'export OMO_WORK_LOGS_ROOT="{parent}"\n', encoding="utf-8")
        local_env.chmod(0o600)
        return local_env

    @staticmethod
    def write_human_approval_scope(source_dir: Path, records: list[MailRecord], approval_file: Path, task_id: str = SOURCE_815_TASK_ID, provenance: str | None = None, quote: str = SOURCE_815_APPROVAL_QUOTE) -> None:
        provenance_value = str(approval_file.resolve()) if provenance is None else provenance
        scope_tasks = "uid\ttask_id\tgmail_msgid\tgmail_thrid\traw_sha256\n" + "".join(
            f"{record.uid}\t{task_id}\t{record.gmail_msgid}\t{record.gmail_thrid}\t{record.raw_sha256}\n" for record in records
        )
        scope_tasks_digest = hashlib.sha256(scope_tasks.encode()).hexdigest()
        scope_sha = hashlib.sha256(f"{provenance_value}\n{scope_tasks_digest}\n".encode()).hexdigest()
        thread_digest = thread_context_digest(records)
        manifest_header = "uid\tsource_mailbox\tuidvalidity\tdate\tgmail_msgid\tgmail_thrid\tmsgid_sha256\traw_sha256\tflags\tlabels\tthread_context_sha256\tscope_tasks_sha256\tscope_sha256\tscope_preparer\tscope_reviewer\tscope_provenance\tbody_bytes\tsubject\n"
        manifest_rows = "".join(
            f"{record.uid}\tINBOX\t9\t{record.date}\t{record.gmail_msgid}\t{record.gmail_thrid}\t{record.msgid_sha256}\t{record.raw_sha256}\t{tsv_value(record.flags)}\t{tsv_value(record.labels)}\t{thread_digest}\t{scope_tasks_digest}\t{scope_sha}\towner\treviewer\t{provenance_value}\t{record.body_bytes}\t{record.subject}\n"
            for record in records
        )
        (source_dir / "scope-tasks.tsv").write_text(scope_tasks, encoding="utf-8")
        (source_dir / "scope.tsv").write_text(
            "scope_sha256\tscope_tasks_sha256\tpreparer\treviewer\tprovenance\n"
            f"{scope_sha}\t{scope_tasks_digest}\towner\treviewer\t{provenance_value}\n",
            encoding="utf-8",
        )
        (source_dir / "manifest.tsv").write_text(manifest_header + manifest_rows, encoding="utf-8")
        (source_dir / "batches.tsv").write_text(export_batches(records, 10), encoding="utf-8")
        approval_sha256 = hashlib.sha256(approval_file.read_bytes()).hexdigest()
        source_binding = f"{records[0].uid}:{records[0].gmail_msgid}:{records[0].gmail_thrid}:{records[0].raw_sha256}"
        (source_dir / "reason.txt").write_text(f"{approval_file.resolve()}\n{approval_sha256}\n{source_binding}\n{quote}\n", encoding="utf-8")
        (source_dir / "scope-tasks.tsv").chmod(0o600)
        (source_dir / "scope.tsv").chmod(0o600)

    @staticmethod
    def source_815_test_binding(record: MailRecord) -> str:
        return f"{record.uid}:{record.gmail_msgid}:{record.gmail_thrid}:{record.raw_sha256}"

    def assert_human_approved_exact_removal_rejected_before_mailbox(
        self,
        record: MailRecord,
        *,
        source_binding: str | None = None,
        task_id: str = SOURCE_815_TASK_ID,
        quote: str = SOURCE_815_APPROVAL_QUOTE,
        approval_sha256: str | None = None,
        approval_file_name: str = SOURCE_815_APPROVAL_FILE,
        local_root: Path | None = None,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) if local_root is None else local_root
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            approval_file = self.write_human_approval(root, quote=quote)
            if approval_file.name != approval_file_name:
                renamed = approval_file.with_name(approval_file_name)
                approval_file.rename(renamed)
                approval_file = renamed
            self.write_human_approval_scope(source_dir, [record], approval_file, task_id=task_id, quote=quote)
            local_env = self.write_local_env_root(root)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = task_id
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = quote
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", approval_sha256 or hashlib.sha256(approval_file.read_bytes()).hexdigest()),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", source_binding or self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=AssertionError("mailbox must not open")),
            ):
                self.assertEqual(2, cmd_trash_superseded(args))

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
                return "OK", [b"7 8"]

        client = Client()
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()):
            self.assertEqual(["7", "8"], manager_candidate_uids(client, "agent@example.test"))
        self.assertEqual(
            [("search", None, "FROM", '"agent@example.test"')],
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
                ("search", None, "FROM", '"me@example.test"'),
                ("search", None, "FROM", '"me@example.test"', "SUBJECT", '"[a]"'),
                ("search", None, "FROM", '"me@example.test"', "SUBJECT", '"[omo_manager]"'),
            ],
            client.calls,
        )

    def test_unread_candidate_uids_uses_unseen_and_exact_sender_in_split_mode(self) -> None:
        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        class Client:
            calls: list[tuple[object, ...]] = []

            def uid(self, command: str, *args: object) -> tuple[str, list[bytes]]:
                self.calls.append((command, *args))
                return "OK", [b"7 8 7"]

        client = Client()
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()):
            self.assertEqual(["7", "8"], manager_unread_candidate_uids(client, "agent@example.test"))
        self.assertEqual(
            [("search", None, "UNSEEN", "FROM", '"agent@example.test"')],
            client.calls,
        )

    def test_agent_unread_records_filters_to_exact_current_target(self) -> None:
        raw_a = self.raw_message("[wl:7] own", "body")
        raw_b = self.raw_message("[wl:8] other", "body")
        client = FakeClient(
            {
                ("search", None, "UNSEEN", "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7,8", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_a),
                        (b"8 (UID 8 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_b),
                    ],
                ),
                ("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"8 (UID 8 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\Inbox))",
                    ],
                ),
            }
        )
        with patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=object()):
            records = agent_unread_records(client, "agent@example.test", "human@example.test", "wl:7", "session-a")
        self.assertEqual(["7"], [record.uid for record in records])

    def test_agent_unread_records_excludes_finally_seen_and_prior_session(self) -> None:
        current = MailRecord("7", "date", "agent@example.test", "human@example.test", "[wl:7] current", "digest", flags="", agent_session_id="session-a")
        read = MailRecord("8", "date", "agent@example.test", "human@example.test", "[wl:7] read", "digest", flags=r"\Seen", agent_session_id="session-a")
        prior = MailRecord("9", "date", "agent@example.test", "human@example.test", "[wl:7] prior", "digest", flags="", agent_session_id="session-b")
        legacy = MailRecord("10", "date", "agent@example.test", "human@example.test", "[wl:7] legacy", "digest", flags="")
        with (
            patch("omo_manager.omo_manager_mail_compress.manager_unread_candidate_uids", return_value=["7", "8", "9", "10"]),
            patch("omo_manager.omo_manager_mail_compress.accepted_manager_headers", return_value=([current, read, prior, legacy], [])),
            patch("omo_manager.omo_manager_mail_compress.unread_records_with_metadata", return_value=[current, read, prior, legacy]),
        ):
            records = agent_unread_records(FakeClient({}), "agent@example.test", "human@example.test", "wl:7", "session-a")
        self.assertEqual(["7", "10"], [record.uid for record in records])

    def test_current_agent_mail_target_preserves_nonzero_pane(self) -> None:
        result = SimpleNamespace(returncode=0, stdout="wl:7.1\n")
        with patch.dict(os.environ, {"TMUX_PANE": "%42"}), patch("omo_manager.omo_manager_mail_compress.subprocess.run", return_value=result):
            self.assertEqual("wl:7.1", current_agent_mail_target())

    def test_current_agent_mail_target_does_not_fall_back_from_stale_pane(self) -> None:
        result = SimpleNamespace(returncode=1, stdout="")
        with (
            patch.dict(os.environ, {"TMUX_PANE": "%42", "OMO_AGENT_TMUX_TARGET": "wl:7"}),
            patch("omo_manager.omo_manager_mail_compress.subprocess.run", return_value=result),
            self.assertRaisesRegex(RuntimeError, "current tmux pane"),
        ):
            current_agent_mail_target()

    def test_current_agent_session_prefers_session_over_different_thread_id(self) -> None:
        session_id = "01a0369c-7895-70f2-ae4b-5f59d920e99a"
        thread_id = "01a04b9d-7895-70f2-ae4b-5f59d920e99a"
        with patch.dict(os.environ, {"CODEX_SESSION_ID": session_id, "CODEX_THREAD_ID": thread_id}):
            self.assertEqual(session_id, current_agent_session_id())

    def test_agent_trash_replaced_moves_only_verified_unread_own_mail(self) -> None:
        client = FakeClient({("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""])})
        source = MailRecord("7", "date", "agent@example.test", "human@example.test", "[wl:7] old", "digest", gmail_msgid="100", gmail_thrid="200", message_id="<old@example.test>", agent_session_id="01a0369c-7895-70f2-ae4b-5f59d920e99a")
        args = SimpleNamespace(uid=["7"], source_uidvalidity="9", replacement_message_id="<new@example.test>", yes=True)
        final = {"7": SimpleNamespace(flags="", gmail_msgid="100", gmail_thrid="200")}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}),
            patch("omo_manager.omo_manager_mail_compress.current_agent_mail_target", return_value="wl:7"),
            patch("omo_manager.omo_manager_mail_compress.current_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.agent_unread_records", return_value=[source]),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "All", r"\Sent": "Sent"}),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[wl:7] new"),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="101"),
            patch("omo_manager.omo_manager_mail_compress.replacement_supersedes_ids", return_value={"<old@example.test>"}),
            patch("omo_manager.omo_manager_mail_compress.replacement_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", side_effect=[["7"], []]),
            patch("omo_manager.omo_manager_mail_compress.fetch_gmail_metadata_records_compatible", return_value=final),
            patch("omo_manager.omo_manager_mail_compress.gmail_message_uids", return_value=["70"]),
        ):
            self.assertEqual(0, cmd_agent_trash_replaced(args))
        self.assertIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_agent_trash_replaced_refuses_source_read_at_mutation_gate(self) -> None:
        client = FakeClient({})
        source = MailRecord("7", "date", "agent@example.test", "human@example.test", "[wl:7] old", "digest", gmail_msgid="100", gmail_thrid="200", message_id="<old@example.test>", agent_session_id="01a0369c-7895-70f2-ae4b-5f59d920e99a")
        args = SimpleNamespace(uid=["7"], source_uidvalidity="9", replacement_message_id="<new@example.test>", yes=True)
        final = {"7": SimpleNamespace(flags=r"\Seen", gmail_msgid="100", gmail_thrid="200")}
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}),
            patch("omo_manager.omo_manager_mail_compress.current_agent_mail_target", return_value="wl:7"),
            patch("omo_manager.omo_manager_mail_compress.current_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.agent_unread_records", return_value=[source]),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "All", r"\Sent": "Sent"}),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[wl:7] new"),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="101"),
            patch("omo_manager.omo_manager_mail_compress.replacement_supersedes_ids", return_value={"<old@example.test>"}),
            patch("omo_manager.omo_manager_mail_compress.replacement_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.fetch_gmail_metadata_records_compatible", return_value=final),
            self.assertRaisesRegex(RuntimeError, "read or changed"),
        ):
            cmd_agent_trash_replaced(args)
        self.assertFalse(any(call[0] == "MOVE" for call in client.uid_calls))

    def test_agent_trash_replaced_refuses_unbound_replacement(self) -> None:
        client = FakeClient({})
        source = MailRecord("7", "date", "agent@example.test", "human@example.test", "[wl:7] old", "digest", gmail_msgid="100", gmail_thrid="200", message_id="<old@example.test>", agent_session_id="01a0369c-7895-70f2-ae4b-5f59d920e99a")
        args = SimpleNamespace(uid=["7"], source_uidvalidity="9", replacement_message_id="<unrelated@example.test>", yes=True)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}),
            patch("omo_manager.omo_manager_mail_compress.current_agent_mail_target", return_value="wl:7"),
            patch("omo_manager.omo_manager_mail_compress.current_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.agent_unread_records", return_value=[source]),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "All", r"\Sent": "Sent"}),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[wl:7] unrelated"),
            patch("omo_manager.omo_manager_mail_compress.replacement_supersedes_ids", return_value=set()),
            self.assertRaisesRegex(RuntimeError, "does not explicitly supersede"),
        ):
            cmd_agent_trash_replaced(args)
        self.assertFalse(any(call[0] == "MOVE" for call in client.uid_calls))

    def test_agent_trash_replaced_refuses_older_replacement(self) -> None:
        client = FakeClient({})
        source = MailRecord("7", "date", "agent@example.test", "human@example.test", "[wl:7] old", "digest", gmail_msgid="100", gmail_thrid="200", message_id="<old@example.test>", agent_session_id="01a0369c-7895-70f2-ae4b-5f59d920e99a")
        args = SimpleNamespace(uid=["7"], source_uidvalidity="9", replacement_message_id="<new@example.test>", yes=True)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}),
            patch("omo_manager.omo_manager_mail_compress.current_agent_mail_target", return_value="wl:7"),
            patch("omo_manager.omo_manager_mail_compress.current_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.agent_unread_records", return_value=[source]),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "All", r"\Sent": "Sent"}),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[wl:7] new"),
            patch("omo_manager.omo_manager_mail_compress.replacement_supersedes_ids", return_value={"<old@example.test>"}),
            patch("omo_manager.omo_manager_mail_compress.replacement_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="99"),
            self.assertRaisesRegex(RuntimeError, "newer"),
        ):
            cmd_agent_trash_replaced(args)
        self.assertFalse(any(call[0] == "MOVE" for call in client.uid_calls))

    def test_agent_trash_replaced_recovers_timeout_after_applied_move(self) -> None:
        first_client = FakeClient({})
        second_client = FakeClient({})
        source = MailRecord("7", "date", "agent@example.test", "human@example.test", "[wl:7] old", "digest", gmail_msgid="100", gmail_thrid="200", message_id="<old@example.test>", agent_session_id="01a0369c-7895-70f2-ae4b-5f59d920e99a")
        args = SimpleNamespace(uid=["7"], source_uidvalidity="9", replacement_message_id="<new@example.test>", yes=True)
        final = {"7": SimpleNamespace(flags="", gmail_msgid="100", gmail_thrid="200")}
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}),
                patch("omo_manager.omo_manager_mail_compress.current_agent_mail_target", return_value="wl:7"),
                patch("omo_manager.omo_manager_mail_compress.current_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(first_client, {})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                patch("omo_manager.omo_manager_mail_compress.agent_unread_records", return_value=[source]),
                patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "All", r"\Sent": "Sent"}),
                patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
                patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
                patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[wl:7] new"),
                patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="101"),
                patch("omo_manager.omo_manager_mail_compress.replacement_supersedes_ids", return_value={"<old@example.test>"}),
                patch("omo_manager.omo_manager_mail_compress.replacement_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
                patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
                patch("omo_manager.omo_manager_mail_compress.fetch_gmail_metadata_records_compatible", return_value=final),
                patch("omo_manager.omo_manager_mail_compress.imap_uid", side_effect=ImapOperationError("move-agent-unread-to-trash", "timeout after apply")),
                self.assertRaisesRegex(ImapOperationError, "timeout after apply"),
            ):
                cmd_agent_trash_replaced(args)
            with (
                patch.dict(os.environ, {"OMO_MANAGER_STATE_DIR": tmp}),
                patch("omo_manager.omo_manager_mail_compress.current_agent_mail_target", return_value="wl:7"),
                patch("omo_manager.omo_manager_mail_compress.current_agent_session_id", return_value="01a0369c-7895-70f2-ae4b-5f59d920e99a"),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(second_client, {})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                patch("omo_manager.omo_manager_mail_compress.agent_move_locations", return_value={"100": "trash"}),
            ):
                self.assertEqual(0, cmd_agent_trash_replaced(args))

    def test_unread_summary_groups_read_only_threads_and_preserves_read_now_fields(self) -> None:
        raw_old = self.raw_message("[wl:1] task", "first body\n> quoted old body")
        raw_latest = self.raw_message("Re: [wl:1] task", "Decision needed now\n\n> prior context")
        raw_other = self.raw_message("[wl:2] other", "other body")
        client = FakeClient(
            {
                ("search", None, "UNSEEN", "FROM", '"agent@example.test"'): ("OK", [b"7 8 9"]),
                ("fetch", "7,8,9", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_old),
                        (b"8 (UID 8 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_latest),
                        (
                            b"9 (UID 9 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}",
                            raw_other.replace(b"Agent <agent@example.test>", b"Other <other@example.test>"),
                        ),
                    ],
                ),
                ("fetch", "7,8", FULL_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[] {1}", raw_old),
                        (b"8 (UID 8 BODY[] {1}", raw_latest),
                    ],
                ),
                ("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"8 (UID 8 FLAGS () X-GM-MSGID 101 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                    ],
                ),
            }
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
            self.assertEqual(0, cmd_unread_summary(UnreadSummaryArgs(max_body_chars=120)))
        summary = json.loads(output.getvalue())
        self.assertEqual("omo-manager-mail-unread-summary/v1", summary["schema"])
        self.assertTrue(summary["read_only"])
        self.assertEqual("9", summary["source_uidvalidity"])
        self.assertEqual(3, summary["candidate_unread_count"])
        self.assertEqual(2, summary["accepted_unread_count"])
        self.assertEqual(["9"], summary["skipped_boundary_mismatch"])
        self.assertEqual(1, summary["thread_count"])
        self.assertEqual(
            {
                "gmail_thread_id": "200",
                "latest_uid": "8",
                "unread_count": 2,
                "included_message_count": 2,
                "uids": ["7", "8"],
                "latest_subject": "Re: [wl:1] task",
                "latest_target": "wl:1",
                "read_now": "UID 8: Decision needed now\n\nUID 7: first body",
            },
            {
                key: summary["threads"][0][key]
                for key in ("gmail_thread_id", "latest_uid", "unread_count", "included_message_count", "uids", "latest_subject", "latest_target", "read_now")
            },
        )
        self.assertNotIn("quoted", summary["threads"][0]["read_now"])
        self.assertLessEqual(len(summary["threads"][0]["read_now"]), 120)
        self.assertTrue(client.logged_out)
        self.assertEqual([], client.select_calls)
        self.assertIn(("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)

    def test_unread_summary_refuses_missing_thread_identity_and_invalid_bounds(self) -> None:
        client = FakeClient({("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-LABELS (\\Inbox))"])})
        with self.assertRaisesRegex(RuntimeError, "incomplete or duplicate UIDs|requires Gmail thread identities"):
            unread_records_with_metadata(client, [MailRecord("7", "", "", "", "subject", "")])  # type: ignore[arg-type]
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=AssertionError("mailbox opened")),
            self.assertRaisesRegex(RuntimeError, "max threads"),
        ):
            cmd_unread_summary(UnreadSummaryArgs(max_threads=0))

    def test_unread_summary_falls_back_when_metadata_batch_is_incomplete(self) -> None:
        raw_old = self.raw_message("[wl:1] task", "first body")
        raw_latest = self.raw_message("Re: [wl:1] task", "Decision needed now")
        client = FakeClient(
            {
                ("search", None, "UNSEEN", "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7,8", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_old),
                        (b"8 (UID 8 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_latest),
                    ],
                ),
                ("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH): ("OK", [b""]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7", gmail_msgid="100", labels=r"\Inbox"),
                ("fetch", "8", GMAIL_METADATA_FETCH): self.gmail_metadata("8", gmail_msgid="101", labels=r"\Inbox"),
                ("fetch", "7,8", FULL_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[] {1}", raw_old),
                        (b"8 (UID 8 BODY[] {1}", raw_latest),
                    ],
                ),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw_old)]),
                ("fetch", "8", FULL_FETCH): ("OK", [(b"message", raw_latest)]),
            }
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
            self.assertEqual(0, cmd_unread_summary(UnreadSummaryArgs(max_body_chars=120)))
        summary = json.loads(output.getvalue())
        self.assertEqual(2, summary["accepted_unread_count"])
        self.assertIn("Decision needed now", summary["threads"][0]["read_now"])
        self.assertIn(("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", GMAIL_METADATA_FETCH), client.uid_calls)
        self.assertIn(("fetch", "8", GMAIL_METADATA_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", FULL_FETCH), client.uid_calls)
        self.assertIn(("fetch", "8", FULL_FETCH), client.uid_calls)

    def test_unread_summary_caps_thread_body_fetches_and_audits_omitted_messages(self) -> None:
        raw_10 = self.raw_message("[wl:1] old", "old body")
        raw_11 = self.raw_message("Re: [wl:1] middle", "middle body " * 30)
        raw_12 = self.raw_message("Re: [wl:1] latest", "latest body " * 30)
        client = FakeClient(
            {
                ("search", None, "UNSEEN", "FROM", '"agent@example.test"'): ("OK", [b"10 11 12"]),
                ("fetch", "10,11,12", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"10 (UID 10 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_10),
                        (b"11 (UID 11 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_11),
                        (b"12 (UID 12 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_12),
                    ],
                ),
                ("fetch", "10,11,12", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"10 (UID 10 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"11 (UID 11 FLAGS () X-GM-MSGID 101 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"12 (UID 12 FLAGS () X-GM-MSGID 102 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                    ],
                ),
                ("fetch", "11,12", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"11 (UID 11 FLAGS () X-GM-MSGID 101 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"12 (UID 12 FLAGS () X-GM-MSGID 102 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                    ],
                ),
                ("fetch", "11,12", FULL_BATCH_FETCH): (
                    "OK",
                    [
                        (b"11 (UID 11 BODY[] {1}", raw_11),
                        (b"12 (UID 12 BODY[] {1}", raw_12),
                    ],
                ),
                ("fetch", "10,11,12", FULL_BATCH_FETCH): ("BAD", [b"must not fetch every unread body"]),
            }
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
            self.assertEqual(0, cmd_unread_summary(UnreadSummaryArgs(max_body_chars=80, max_messages_per_thread=2)))
        thread = json.loads(output.getvalue())["threads"][0]
        self.assertEqual(3, thread["unread_count"])
        self.assertEqual(2, thread["included_message_count"])
        self.assertEqual(1, thread["omitted_older_unread_count"])
        self.assertEqual(["11", "12"], thread["uids"])
        self.assertEqual(["Re: [wl:1] middle", "Re: [wl:1] latest"], thread["subjects"])
        self.assertLessEqual(len(thread["read_now"]), 80)
        self.assertTrue(thread["read_now"].startswith("UID 12: latest body"))
        self.assertRegex(thread["all_unread_uid_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn(("fetch", "11,12", FULL_BATCH_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "10,11,12", FULL_BATCH_FETCH), client.uid_calls)

    def test_snapshot_and_export_header_filter_keeps_pb_newsletter_only(self) -> None:
        def raw(subject: str) -> bytes:
            return (f"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nSubject: {subject}\r\nMessage-ID: <one@example.test>\r\n\r\n").encode()

        client = FakeClient(
            {
                ("fetch", "1,2,3,4", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"1 (UID 1 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw("PB newsletter")),
                        (b"2 (UID 2 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw("PB news")),
                        (b"3 (UID 3 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw("PB stock watch")),
                        (b"4 (UID 4 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw("PB urgent")),
                    ],
                ),
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
        self.assertEqual([("fetch", "1,2,3,4", HEADER_BATCH_FETCH)], client.uid_calls)

    def test_snapshot_header_batch_rejects_missing_uid(self) -> None:
        raw = b"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nSubject: useful\r\nMessage-ID: <one@example.test>\r\n\r\n"
        client = FakeClient(
            {
                ("fetch", "1,2", HEADER_BATCH_FETCH): (
                    "OK",
                    [(b"1 (UID 1 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)],
                ),
            }
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete or duplicate UIDs"):
            fetch_header_records(client, ["1", "2"])  # type: ignore[arg-type]

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

    def test_connect_interrupt_closes_accepted_client_before_handoff(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.shutdown_calls = 0

            def shutdown(self) -> None:
                self.shutdown_calls += 1

        class SyncThread:
            def __init__(self, target: object, name: str, daemon: bool) -> None:
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                self.target()  # type: ignore[operator]

        class InterruptingCondition:
            def __enter__(self) -> object:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def notify(self) -> None:
                return None

            def wait_for(self, _predicate: object, timeout: float) -> bool:
                raise KeyboardInterrupt

        client = Client()

        with (
            patch("omo_manager.omo_manager_mail_compress.imaplib.IMAP4_SSL", return_value=client),
            patch("omo_manager.omo_manager_mail_compress.threading.Thread", SyncThread),
            patch("omo_manager.omo_manager_mail_compress.threading.Condition", InterruptingCondition),
            self.assertRaises(KeyboardInterrupt),
        ):
            connect_mailbox("imap.example.test")
        self.assertEqual(1, client.shutdown_calls)

    def test_connect_observer_receives_client_before_handoff(self) -> None:
        class Client:
            pass

        client = Client()
        observed: list[Client] = []
        with patch("omo_manager.omo_manager_mail_compress.imaplib.IMAP4_SSL", return_value=client):
            self.assertIs(client, connect_mailbox("imap.example.test", observed.append))
        self.assertEqual([client], observed)

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
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))"]),
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

    def test_identity_preflight_batches_headers_and_gmail_metadata(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [(b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))", b"")]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))"]),
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
        self.assertIn("gate=pass", output.getvalue())
        self.assertIn(("fetch", "7", HEADER_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "70", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "7", FULL_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "70", FULL_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "70", FULL_BATCH_FETCH), client.uid_calls)

    def test_snapshot_uses_sender_search_and_batched_headers(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
            }
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
            self.assertEqual(0, cmd_snapshot(Args()))
        self.assertIn("manager_candidate_count=1", output.getvalue())
        self.assertIn(("search", None, "FROM", '"agent@example.test"'), client.uid_calls)
        self.assertNotIn(("search", None, "ALL"), client.uid_calls)
        self.assertIn(("fetch", "7", HEADER_BATCH_FETCH), client.uid_calls)
        self.assertFalse(any(isinstance(arg, str) and arg.startswith("OR") for call in client.uid_calls for arg in call))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_identity_preflight_searches_each_thread_without_union_syntax(self) -> None:
        raw_a = self.raw_message("[worker:0] a")
        raw_b = self.raw_message("[worker:1] b")
        or_query = gmail_thrid_or_query(["200", "201"])
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7,8", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_a),
                        (b"8 (UID 8 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_b),
                    ],
                ),
                ("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"8 (UID 8 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\Inbox))",
                    ],
                ),
                ("search", None, or_query): ("OK", [b"70 71"]),
                ("fetch", "70,71", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))",
                        b"71 (UID 71 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\All))",
                    ],
                ),
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
        self.assertIn("complete_thread_count=2", output.getvalue())
        self.assertIn("gate=pass", output.getvalue())
        self.assertIn(("search", None, or_query), client.uid_calls)
        self.assertNotIn(("search", None, "X-GM-THRID", "200"), client.uid_calls)
        self.assertNotIn(("search", None, "X-GM-THRID", "201"), client.uid_calls)
        self.assertFalse(any(arg == "X-GM-RAW" for call in client.uid_calls for arg in call))
        self.assertNotIn(("search", None, "ALL"), client.uid_calls)
        self.assertIn(("fetch", "70,71", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertEqual(1, client.uid_calls.count(("fetch", "70,71", GMAIL_METADATA_BATCH_FETCH)))
        self.assertNotIn(("fetch", "70", FULL_BATCH_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "71", FULL_BATCH_FETCH), client.uid_calls)
        self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)
        self.assertNotIn(('"[Gmail]/Sent Mail"', True), client.select_calls)
        self.assertTrue(all(readonly for _mailbox, readonly in client.select_calls))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_identity_preflight_requires_all_mail_and_sent(self) -> None:
        raw = self.raw_message("[worker:0] a")
        inbox = {
            ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
            ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
            ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"]),
            ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
            ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))"]),
        }

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        for mailboxes in (
            [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"'],
            [b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"'],
        ):
            client = FakeClient(inbox, mailboxes)
            output = io.StringIO()
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
                redirect_stdout(output),
            ):
                self.assertEqual(1, cmd_identity_preflight(Args()))
            self.assertIn("gate=block", output.getvalue())
            self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_identity_preflight_blocks_when_source_msgid_absent_from_all_mail_thread(self) -> None:
        raw = self.raw_message("[worker:0] a")
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"70 (UID 70 FLAGS () X-GM-MSGID 999 X-GM-THRID 200 X-GM-LABELS (\\All))"]),
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
            self.assertEqual(1, cmd_identity_preflight(Args()))
        self.assertIn("gate=block", output.getvalue())
        self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)

    def test_identity_preflight_falls_back_from_nested_or_to_per_thread(self) -> None:
        raw = self.raw_message("[worker:0] a")
        raw_b = self.raw_message("[worker:1] b")
        or_query = gmail_thrid_or_query(["200", "201"])
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7,8", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw),
                        (b"8 (UID 8 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_b),
                    ],
                ),
                ("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"8 (UID 8 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\Inbox))",
                    ],
                ),
                ("search", None, or_query): ("NO", [b"parse"]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("search", None, "X-GM-THRID", "201"): ("OK", [b"71"]),
                ("fetch", "70,71", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))",
                        b"71 (UID 71 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\All))",
                    ],
                ),
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
        self.assertIn("gate=pass", output.getvalue())
        self.assertIn(("search", None, or_query), client.uid_calls)
        self.assertIn(("search", None, "X-GM-THRID", "200"), client.uid_calls)
        self.assertIn(("search", None, "X-GM-THRID", "201"), client.uid_calls)
        self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)

    def test_discover_spans_or_batches(self) -> None:
        records = [
            MailRecord("7", "date", "agent", "human", "s", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64),
            MailRecord("8", "date", "agent", "human", "s", "msg-b", gmail_msgid="101", gmail_thrid="201", raw_sha256="b" * 64),
            MailRecord("9", "date", "agent", "human", "s", "msg-c", gmail_msgid="102", gmail_thrid="202", raw_sha256="c" * 64),
        ]
        or_query = gmail_thrid_or_query(["200", "201"])
        client = FakeClient(
            {
                ("search", None, or_query): ("OK", [b"70 71"]),
                ("search", None, "X-GM-THRID", "202"): ("OK", [b"72"]),
            },
            self.gmail_mailboxes(),
        )
        with patch("omo_manager.omo_manager_mail_compress.GMAIL_THREAD_OR_BATCH", 2):
            _special_use, uids = discover_gmail_thread_member_uids(client, records)
        self.assertEqual(["70", "71", "72"], uids)
        self.assertIn(("search", None, or_query), client.uid_calls)
        self.assertIn(("search", None, "X-GM-THRID", "202"), client.uid_calls)
        self.assertNotIn(("search", None, "X-GM-THRID", "200"), client.uid_calls)
        self.assertNotIn(("search", None, "X-GM-THRID", "201"), client.uid_calls)
        self.assertFalse(any(arg == "X-GM-RAW" for call in client.uid_calls for arg in call))
        self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)
        self.assertNotIn(('"[Gmail]/Sent Mail"', True), client.select_calls)

    def test_discover_rejects_duplicate_uid_across_or_batches(self) -> None:
        records = [
            MailRecord("7", "date", "agent", "human", "s", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64),
            MailRecord("8", "date", "agent", "human", "s", "msg-b", gmail_msgid="101", gmail_thrid="201", raw_sha256="b" * 64),
            MailRecord("9", "date", "agent", "human", "s", "msg-c", gmail_msgid="102", gmail_thrid="202", raw_sha256="c" * 64),
        ]
        client = FakeClient(
            {
                ("search", None, gmail_thrid_or_query(["200", "201"])): ("OK", [b"70 71"]),
                ("search", None, "X-GM-THRID", "202"): ("OK", [b"70"]),
            },
            self.gmail_mailboxes(),
        )
        with (
            patch("omo_manager.omo_manager_mail_compress.GMAIL_THREAD_OR_BATCH", 2),
            self.assertRaisesRegex(RuntimeError, "duplicate UID"),
        ):
            discover_gmail_thread_member_uids(client, records)

    def test_identity_preflight_blocks_extra_thrid_in_union(self) -> None:
        raw_a = self.raw_message("[worker:0] a")
        raw_b = self.raw_message("[worker:1] b")
        or_query = gmail_thrid_or_query(["200", "201"])
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7 8"]),
                ("fetch", "7,8", HEADER_BATCH_FETCH): (
                    "OK",
                    [
                        (b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_a),
                        (b"8 (UID 8 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw_b),
                    ],
                ),
                ("fetch", "7,8", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))",
                        b"8 (UID 8 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\Inbox))",
                    ],
                ),
                ("search", None, or_query): ("OK", [b"70 71 72"]),
                ("fetch", "70,71,72", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))",
                        b"71 (UID 71 FLAGS () X-GM-MSGID 101 X-GM-THRID 201 X-GM-LABELS (\\All))",
                        b"72 (UID 72 FLAGS () X-GM-MSGID 999 X-GM-THRID 999 X-GM-LABELS (\\All))",
                    ],
                ),
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
            self.assertEqual(1, cmd_identity_preflight(Args()))
        self.assertIn("gate=block", output.getvalue())
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_thread_context_rejects_duplicate_uid_across_threads(self) -> None:
        records = [
            MailRecord("7", "date", "agent", "human", "s", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64),
            MailRecord("8", "date", "agent", "human", "s", "msg-b", gmail_msgid="101", gmail_thrid="201", raw_sha256="b" * 64),
        ]
        client = FakeClient(
            {
                ("search", None, gmail_thrid_or_query(["200", "201"])): ("NO", [b"parse"]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("search", None, "X-GM-THRID", "201"): ("OK", [b"70"]),
            },
            self.gmail_mailboxes(),
        )
        with self.assertRaisesRegex(RuntimeError, "duplicate UID"):
            fetch_imap_thread_contexts(client, records)
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_thread_search_rejects_duplicate_uid_in_one_response(self) -> None:
        records = [
            MailRecord("7", "date", "agent", "human", "s", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64),
        ]
        client = FakeClient(
            {("search", None, "X-GM-THRID", "200"): ("OK", [b"70 70"])},
            self.gmail_mailboxes(),
        )
        with self.assertRaisesRegex(RuntimeError, "incomplete or duplicate UIDs"):
            fetch_imap_thread_contexts(client, records)

    def test_thread_context_falls_back_when_metadata_batch_is_incomplete(self) -> None:
        raw = self.raw_message("[worker:0] a")
        records = [MailRecord("7", "date", "agent", "human", "s", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)]
        client = FakeClient(
            {
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70"]),
                ("fetch", "70", FULL_BATCH_FETCH): ("OK", [(b"70 (UID 70 BODY[] {1}", raw)]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b""]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70", labels=r"\All"),
            },
            self.gmail_mailboxes(),
        )
        _mailboxes, by_thread = fetch_imap_thread_contexts(client, records)
        self.assertEqual(["100"], [record.gmail_msgid for record in by_thread["200"]])
        self.assertIn(("fetch", "70", FULL_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "70", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "70", FULL_FETCH), client.uid_calls)
        self.assertIn(("fetch", "70", GMAIL_METADATA_FETCH), client.uid_calls)

    def test_full_records_reject_duplicate_full_batch_uid(self) -> None:
        raw = self.raw_message("[worker:0] a")
        client = FakeClient(
            {
                ("fetch", "70", FULL_BATCH_FETCH): (
                    "OK",
                    [
                        (b"70 (UID 70 BODY[] {1}", raw),
                        (b"70 (UID 70 BODY[] {1}", raw),
                    ],
                ),
            },
            self.gmail_mailboxes(),
        )

        with self.assertRaisesRegex(RuntimeError, "duplicate UID"):
            fetch_full_records(client, ["70"])
        self.assertIn(("fetch", "70", FULL_BATCH_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "70", FULL_FETCH), client.uid_calls)

    def test_full_records_reject_duplicate_metadata_batch_uid(self) -> None:
        raw = self.raw_message("[worker:0] a")
        client = FakeClient(
            {
                ("fetch", "70", FULL_BATCH_FETCH): ("OK", [(b"70 (UID 70 BODY[] {1}", raw)]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))",
                        b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))",
                    ],
                ),
            },
            self.gmail_mailboxes(),
        )

        with self.assertRaisesRegex(RuntimeError, "duplicate UID"):
            fetch_full_records(client, ["70"])
        self.assertIn(("fetch", "70", FULL_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "70", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "70", FULL_FETCH), client.uid_calls)

    def test_identity_preflight_falls_back_from_all_mail_metadata_batch(self) -> None:
        raw = self.raw_message("[worker:0] a")
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70 71"]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))"]),
                ("fetch", "71", GMAIL_METADATA_BATCH_FETCH): ("OK", [b""]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="102", labels=r"\All"),
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
            patch("omo_manager.omo_manager_mail_compress.GMAIL_IDENTITY_UID_BATCH", 1),
            redirect_stdout(output),
        ):
            self.assertEqual(0, cmd_identity_preflight(Args()))
        self.assertIn("gate=pass", output.getvalue())
        self.assertIn(("fetch", "70", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "71", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "71", GMAIL_METADATA_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "71", FULL_FETCH), client.uid_calls)
        self.assertIn(('"[Gmail]/All Mail"', True), client.select_calls)

    def test_identity_preflight_blocks_malformed_fallback_msgid(self) -> None:
        raw = self.raw_message("[worker:0] a")
        client = FakeClient(
            {
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"]),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70 71"]),
                ("fetch", "70", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))"]),
                ("fetch", "71", GMAIL_METADATA_BATCH_FETCH): ("OK", [b""]),
                ("fetch", "71", GMAIL_METADATA_FETCH): self.gmail_metadata("71", gmail_msgid="not-a-number", labels=r"\All"),
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
            patch("omo_manager.omo_manager_mail_compress.GMAIL_IDENTITY_UID_BATCH", 1),
            redirect_stdout(output),
        ):
            self.assertEqual(1, cmd_identity_preflight(Args()))
        self.assertIn("gate=block", output.getvalue())
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_identity_preflight_fetch_timeout_names_stage_and_blocks(self) -> None:
        raw = self.raw_message("[worker:0] complete")

        class BlockingSocket:
            def shutdown(self, _how: int) -> None:
                pass

            def close(self) -> None:
                pass

        mailboxes = self.gmail_mailboxes()

        class BlockingThreadSearchClient(FakeClient):
            def __init__(self) -> None:
                super().__init__(
                    {
                        ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                        ("fetch", "7", HEADER_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID)] {1}", raw)]),
                        ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"]),
                    },
                    mailboxes,
                )
                self.sock = BlockingSocket()
                self.release = threading.Event()

            def uid(self, *args: str) -> tuple[str, list[bytes | tuple[bytes, bytes]]]:
                if args == ("search", None, "X-GM-THRID", "200"):
                    self.uid_calls.append(args)
                    self.release.wait()
                    raise OSError("stub remains blocked past socket close")
                return super().uid(*args)

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        client = BlockingThreadSearchClient()
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
        self.assertIn("failed_stage=gmail-thread-search thread=200", errors.getvalue())
        self.assertEqual(1, client.uid_calls.count(("search", None, "X-GM-THRID", "200")))
        self.assertFalse(any(call[0].casefold() in {"copy", "move", "store", "expunge"} for call in client.uid_calls))

    def test_export_writes_gmail_context_and_special_mailboxes(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        other_raw = b"From: Other <other@example.test>\r\nTo: Human <human@example.test>\r\nSubject: [worker:0] complete\r\nMessage-ID: <two@example.test>\r\n\r\nother context\r\n"
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [b"7 (UID 7 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\Inbox))"],
                ),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
                ("search", None, "X-GM-THRID", "200"): ("OK", [b"70 71"]),
                ("fetch", "70,71", FULL_BATCH_FETCH): (
                    "OK",
                    [(b"70 (UID 70 BODY[] {1}", raw), (b"71 (UID 71 BODY[] {1}", other_raw)],
                ),
                ("fetch", "70,71", GMAIL_METADATA_BATCH_FETCH): (
                    "OK",
                    [
                        b"70 (UID 70 FLAGS () X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS (\\All))",
                        b"71 (UID 71 FLAGS () X-GM-MSGID 101 X-GM-THRID 200 X-GM-LABELS (\\All))",
                    ],
                ),
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
        self.assertIn(("fetch", "7", FULL_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "7", FULL_FETCH), client.uid_calls)
        self.assertNotIn(("fetch", "7", GMAIL_METADATA_FETCH), client.uid_calls)

    def test_export_falls_back_when_metadata_batch_is_incomplete(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
                ("fetch", "7", HEADER_FETCH): ("OK", [(b"header", raw)]),
                ("fetch", "7", FULL_BATCH_FETCH): ("OK", [(b"7 (UID 7 BODY[] {1}", raw)]),
                ("fetch", "7", GMAIL_METADATA_BATCH_FETCH): ("OK", [b""]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", raw)]),
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
        self.assertIn(("fetch", "7", FULL_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", GMAIL_METADATA_BATCH_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", FULL_FETCH), client.uid_calls)
        self.assertIn(("fetch", "7", GMAIL_METADATA_FETCH), client.uid_calls)

    def test_export_retries_missing_full_fetch_for_only_the_frozen_uid(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        client = FakeClient(
            {
                ("search", None, "ALL"): ("OK", [b"7"]),
                ("search", None, "FROM", '"agent@example.test"'): ("OK", [b"7"]),
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
        self.assertNotIn(("search", None, "ALL"), client.uid_calls)
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
        self.assertNotIn(("search", None, "ALL"), client.uid_calls)
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
                self.assertEqual(0, cmd_verify_run(VerifyArgs(source_dir)))
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

    def test_reconcile_source_815_exact_removal_intent_uses_exact_trash_evidence(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "X-GM-MSGID", "100"): [("OK", [b""]), ("OK", [b"70"]), ("OK", [b""]), ("OK", [b"70"])],
                ("fetch", "70", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "70", GMAIL_METADATA_FETCH): [self.gmail_metadata("70", labels=r"\Trash"), self.gmail_metadata("70", labels=r"\Trash")],
            },
            self.gmail_mailboxes(),
        )
        approval_sha256 = "a" * 64
        exact_removal = ExactRemovalEvidence(
            exception=SOURCE_815_EXACT_REMOVAL_EXCEPTION,
            approval_sha256=approval_sha256,
            approval_quote_sha256=SOURCE_815_APPROVAL_QUOTE_SHA256,
            approval_source_binding=self.source_815_test_binding(record),
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
                SOURCE_815_TASK_ID,
                "reviewer-2",
                exact_removal,
            )
            with (
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", approval_sha256),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                patch("omo_manager.omo_manager_mail_compress.reconciliation_thread_unchanged", side_effect=AssertionError("source-815 recovery should use exact evidence")),
            ):
                self.assertEqual(0, cmd_reconcile_intent(ReconcileArgs(source_dir)))
            self.assertTrue((source_dir / "outcomes" / "200.tsv").exists())
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
                with self.assertRaisesRegex(RuntimeError, "exactly one"):
                    cmd_verify_run(VerifyArgs(source_dir))
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

    def test_replacement_lookup_can_restore_inbox_readonly(self) -> None:
        raw = b"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\nMessage-ID: <replacement@example.test>\r\n\r\nsummary\r\n"
        client = FakeClient({
            ("search", None, "HEADER", "Message-ID", '"<replacement@example.test>"'): ("OK", [b"90"]),
            ("fetch", "90", HEADER_FETCH): ("OK", [(b"header", raw)]),
        })
        self.assertTrue(replacement_exists(client, "[Gmail]/All Mail", "<replacement@example.test>", "agent@example.test", "human@example.test", restore_readonly=True))
        self.assertEqual(('"INBOX"', True), client.select_calls[-1])

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

    def test_trash_explicit_accepts_only_exact_in_memory_source_bindings(self) -> None:
        digest = "a" * 64
        source = parse_explicit_source(f"7:100:200:{digest}")
        self.assertEqual(("7", "100", "200", digest), (source.uid, source.gmail_msgid, source.gmail_thrid, source.raw_sha256))
        with self.assertRaisesRegex(ValueError, "UID:GMAIL-MSGID"):
            parse_explicit_source("7:100:200")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            parse_explicit_source("7:100:200:not-a-digest")

    def test_trash_explicit_help_requires_no_evidence_directory(self) -> None:
        helper = Path(__file__).parents[1] / "omo_manager_mail_compress.py"
        result = subprocess.run([helper, "trash-explicit", "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--source UID:GMAIL-MSGID:GMAIL-THRID:RAW-SHA256", result.stdout)
        self.assertIn("--context GMAIL-MSGID:GMAIL-THRID:RAW-SHA256", result.stdout)
        self.assertIn("--route-resolution TASK-ID=SESSION:WINDOW[.PANE]", result.stdout)
        self.assertIn("--replacement-not-required", result.stdout)
        self.assertIn("--allow-additive-final-context", result.stdout)
        self.assertIn("--strict-fresh", result.stdout)
        self.assertIn("--recover-partial-move", result.stdout)
        self.assertIn("Explicit interrupted-operation recovery mode", result.stdout)
        self.assertIn("--retained-replacement UID:GMAIL-MSGID:GMAIL-THRID:RAW-SHA256:BODY-BYTES:BODY-SHA256", result.stdout)
        self.assertIn("Required reviewed retained Inbox binding", result.stdout)
        self.assertIn("rejects additions by default", result.stdout)
        self.assertIn("sequential, non-atomic final read gate", result.stdout)
        self.assertIn("final_gate_atomic=0", result.stdout)
        self.assertNotIn("--source-dir", result.stdout)
        self.assertNotIn("--uid-file", result.stdout)

    def test_trash_explicit_direct_caller_requires_retained_binding(self) -> None:
        digest = "a" * 64
        args = argparse.Namespace(
            yes=True,
            source=[f"7:100:200:{digest}"],
            context=[f"100:200:{digest}"],
            replacement_id=["<replacement@example.test>"],
            task_id=["task-a"],
            preparer="owner-a",
            reviewer="reviewer-b",
            task_source=["1:100"],
            route_resolution=[],
            source_uidvalidity="1",
        )
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_trash_explicit_replacement_free_requires_source_1140_before_mailbox(self) -> None:
        digest = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{digest}"],
                "context": [f"100:200:{digest}"],
                "replacement_id": [],
                "replacement_not_required": True,
                "human_approval_file": None,
                "human_approval_quote": None,
                "independent_review_file": None,
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "route_resolution": [],
                "source_uidvalidity": "1",
            },
        )()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_source_1140_direct_removal_binds_exact_reviewed_operation(self) -> None:
        digest = "a" * 64
        source = parse_explicit_source(f"7:100:200:{digest}")
        context = parse_explicit_context(f"100:200:{digest}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail_root = root / "manager_mail"
            mail_root.mkdir(mode=0o700)
            approval = mail_root / SOURCE_1140_APPROVAL_FILE
            approval.write_text(SOURCE_1140_APPROVAL_QUOTE, encoding="utf-8")
            approval.chmod(0o600)
            approval_sha256 = hashlib.sha256(approval.read_bytes()).hexdigest()
            review = root / "review.tsv"
            review.write_text(
                "kind\tvalue\n"
                "version\tv1.0.0\n"
                f"approval_sha256\t{approval_sha256}\n"
                "task_id\ttask-a\n"
                "preparer\towner-a\n"
                "reviewer\treviewer-b\n"
                "verdict\tPASS\n"
                f"source\t7:100:200:{digest}\n"
                f"context\t100:200:{digest}\n",
                encoding="utf-8",
            )
            review.chmod(0o600)
            with (
                patch("omo_manager.omo_manager_mail_compress.configured_work_logs_root", return_value=root),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_1140_APPROVAL_SHA256", approval_sha256),
            ):
                require_source_1140_direct_removal(
                    approval,
                    SOURCE_1140_APPROVAL_QUOTE,
                    review,
                    "task-a",
                    [source],
                    [context],
                    "owner-a",
                    "reviewer-b",
                )
                with self.assertRaisesRegex(RuntimeError, "does not match the exact operation"):
                    require_source_1140_direct_removal(
                        approval,
                        SOURCE_1140_APPROVAL_QUOTE,
                        review,
                        "task-b",
                        [source],
                        [context],
                        "owner-a",
                        "reviewer-b",
                    )

    def test_inspect_explicit_requires_task_and_uids_before_mailbox_access(self) -> None:
        args = type("InspectArgs", (), {"uids": "", "task_id": "task-a"})()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_inspect_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_inspect_explicit_help_has_no_output_directory(self) -> None:
        helper = Path(__file__).parents[1] / "omo_manager_mail_compress.py"
        result = subprocess.run([helper, "inspect-explicit", "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--uids UIDS", result.stdout)
        self.assertIn("--task-id TASK_ID", result.stdout)
        self.assertNotIn("--out-dir", result.stdout)

    def test_inspect_explicit_prints_complete_live_bindings_and_body_readonly(self) -> None:
        source = MailRecord(
            "7",
            "date",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "Re: [wl:7.0] task update",
            "msg-a",
            body="complete body",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256="a" * 64,
        )
        prior = replace(source, uid="8", subject="Re: [wl:7] prior context", body="prior complete body", gmail_msgid="101", raw_sha256="b" * 64)
        args = type("InspectArgs", (), {"uids": "7", "task_id": "task-a"})()
        client = object()
        stdout = io.StringIO()
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})) as open_mailbox_mock,
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="9"),
            patch("omo_manager.omo_manager_mail_compress.fetch_records", return_value=[source]),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source, prior]}),
            patch("omo_manager.omo_manager_mail_compress.logout_mailbox"),
            redirect_stdout(stdout),
        ):
            self.assertEqual(0, cmd_inspect_explicit(args))
        open_mailbox_mock.assert_called_once_with(readonly=True)
        output = stdout.getvalue()
        self.assertIn(f"source=7:100:200:{'a' * 64}", output)
        self.assertIn(f"context=100:200:{'a' * 64}", output)
        self.assertIn(f"context=101:200:{'b' * 64}", output)
        self.assertIn("context_date=date", output)
        self.assertIn("context_from=Agent <agent@example.test>", output)
        self.assertIn("context_to=Human <human@example.test>", output)
        self.assertEqual(2, output.count("context_sender_tmux_target=wl:7"))
        self.assertIn("selected_source_sender_tmux_target=wl:7", output)
        self.assertIn("prior complete body", output)
        self.assertIn("Source-UIDVALIDITY: 9", output)
        self.assertIn("complete body", output)

    def test_inspect_explicit_rejects_boundary_mismatch(self) -> None:
        source = MailRecord("7", "date", "other@example.test", "human@example.test", "subject", "msg", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(object(), {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="9"),
            patch("omo_manager.omo_manager_mail_compress.fetch_records", return_value=[source]),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts") as contexts_mock,
            patch("omo_manager.omo_manager_mail_compress.logout_mailbox"),
        ):
            with self.assertRaisesRegex(RuntimeError, "manager-mail boundary"):
                cmd_inspect_explicit(type("InspectArgs", (), {"uids": "7", "task_id": "task-a"})())
        contexts_mock.assert_not_called()

    def test_direct_inspection_context_includes_all_mail_and_trash(self) -> None:
        source = MailRecord("7", "date", "agent", "human", "subject", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)
        trashed = MailRecord("70", "date", "agent", "human", "old", "msg-b", gmail_msgid="101", gmail_thrid="200", raw_sha256="b" * 64)
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.fetch_imap_thread_contexts", return_value=({}, {"200": [source]})),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox") as select_mock,
            patch("omo_manager.omo_manager_mail_compress.gmail_thread_uids", return_value=["70"]),
            patch("omo_manager.omo_manager_mail_compress.fetch_records", return_value=[trashed]),
        ):
            contexts = fetch_direct_thread_contexts(client, [source])
        select_mock.assert_called_once_with(client, "[Gmail]/Trash", readonly=True)
        self.assertEqual(["100", "101"], [record.gmail_msgid for record in contexts["200"]])

    def test_locate_replacement_rejects_empty_subject_before_mailbox_access(self) -> None:
        args = type("LocateArgs", (), {"subject": ""})()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_locate_replacement(args))
        open_mailbox_mock.assert_not_called()

    def test_locate_replacement_prints_unique_exact_message_id(self) -> None:
        record = MailRecord("7", "date", "agent", "human", "exact subject", "digest")
        msg = Message()
        msg["Message-ID"] = "<replacement@example.test>"
        stdout = io.StringIO()
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(object(), {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail"}),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox") as select_mock,
            patch("omo_manager.omo_manager_mail_compress.manager_candidate_uids", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.accepted_manager_headers", return_value=([record], [])),
            patch("omo_manager.omo_manager_mail_compress.fetch_msg", return_value=(msg, "a" * 64)),
            patch("omo_manager.omo_manager_mail_compress.logout_mailbox"),
            redirect_stdout(stdout),
        ):
            self.assertEqual(0, cmd_locate_replacement(type("LocateArgs", (), {"subject": "exact subject"})()))
        select_mock.assert_called_once_with(select_mock.call_args.args[0], "[Gmail]/All Mail", readonly=True)
        self.assertIn("message_id=<replacement@example.test>", stdout.getvalue())

    def test_locate_replacement_rejects_duplicate_exact_subject(self) -> None:
        record = MailRecord("7", "date", "agent", "human", "exact subject", "digest")
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(object(), {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail"}),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.manager_candidate_uids", return_value=["7", "8"]),
            patch("omo_manager.omo_manager_mail_compress.accepted_manager_headers", return_value=([record, replace(record, uid="8")], [])),
            patch("omo_manager.omo_manager_mail_compress.fetch_msg") as fetch_mock,
            patch("omo_manager.omo_manager_mail_compress.logout_mailbox"),
        ):
            self.assertEqual(1, cmd_locate_replacement(type("LocateArgs", (), {"subject": "exact subject"})()))
        fetch_mock.assert_not_called()

    def test_trash_explicit_rejects_whitespace_identity_variants(self) -> None:
        digest = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{digest}"],
                "context": [f"100:200:{digest}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner",
                "reviewer": "owner ",
            },
        )()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_trash_explicit_requires_distinct_preparer_and_reviewer(self) -> None:
        digest = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{digest}"],
                "context": [f"100:200:{digest}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "same",
                "reviewer": "same",
            },
        )()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_trash_explicit_rejects_bad_binding_before_opening_mailbox(self) -> None:
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": ["7:100:200:bad"],
                "context": [],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_trash_explicit_rejects_source_bound_to_another_task(self) -> None:
        digest = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"task-b:7:100:200:{digest}"],
                "context": [f"7:100:200:{digest}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_direct_thread_revalidation_allows_only_additive_context(self) -> None:
        baseline = MailRecord("7", "", "", "", "", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)
        later = MailRecord("8", "", "", "", "", "msg-b", gmail_msgid="101", gmail_thrid="200", raw_sha256="b" * 64)
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.gmail_thread_uids", return_value=["7", "8"]),
            patch("omo_manager.omo_manager_mail_compress.fetch_records", side_effect=[[baseline, later], [], [baseline, later], []]),
        ):
            context = parse_explicit_context(f"100:200:{'a' * 64}")
            self.assertFalse(direct_context_intact(client, "[Gmail]/All Mail", [context], allow_additive=False))
            self.assertTrue(direct_context_intact(client, "[Gmail]/All Mail", [context], allow_additive=True))
            changed = MailRecord("7", "", "", "", "", "msg-a", gmail_msgid="100", gmail_thrid="200", raw_sha256="c" * 64)
            with patch("omo_manager.omo_manager_mail_compress.fetch_records", side_effect=[[changed, later], []]):
                self.assertFalse(direct_context_intact(client, "[Gmail]/All Mail", [context], allow_additive=True))

    def test_batched_final_context_rejects_arrival_between_trash_and_all_reads(self) -> None:
        source = MailRecord("7", "", "", "", "", "source", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)
        later = replace(source, uid="8", gmail_msgid="101", raw_sha256="b" * 64)
        events: list[str] = []

        def inject_arrival(name: str) -> None:
            events.append(name)

        def records_after_event(_client: FakeClient, _uids: list[str]) -> list[MailRecord]:
            return [source, later] if "contexts-trash" in events else []

        with (
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.gmail_thread_uids_union", side_effect=[["70"], ["7", "8"]]),
            patch("omo_manager.omo_manager_mail_compress.fetch_full_records", side_effect=records_after_event),
            patch("omo_manager.omo_manager_mail_compress.final_gate_event", side_effect=inject_arrival),
        ):
            self.assertFalse(
                direct_contexts_intact(
                    FakeClient({}),
                    "[Gmail]/All Mail",
                    {"200": [parse_explicit_context(f"100:200:{'a' * 64}")]},
                    allow_additive=False,
                )
            )
        self.assertEqual(["contexts-trash", "contexts-all"], events)

    def test_batched_final_context_rejects_arrival_after_earlier_thread_baseline(self) -> None:
        first = MailRecord("7", "", "", "", "", "first", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)
        second = MailRecord("8", "", "", "", "", "second", gmail_msgid="101", gmail_thrid="201", raw_sha256="b" * 64)
        later = replace(first, uid="9", gmail_msgid="102", raw_sha256="c" * 64)
        expected = {
            "200": [parse_explicit_context(f"100:200:{'a' * 64}")],
            "201": [parse_explicit_context(f"101:201:{'b' * 64}")],
        }
        events: list[str] = []

        def records_after_earlier_observation(_client: FakeClient, _uids: list[str]) -> list[MailRecord]:
            return [first, second, later] if "contexts-trash" in events else []

        with (
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.gmail_thread_uids_union", side_effect=[[], ["7", "8", "9"]]),
            patch("omo_manager.omo_manager_mail_compress.fetch_full_records", side_effect=records_after_earlier_observation),
            patch("omo_manager.omo_manager_mail_compress.final_gate_event", side_effect=events.append),
        ):
            self.assertFalse(direct_contexts_intact(FakeClient({}), "[Gmail]/All Mail", expected, allow_additive=False))

    def test_final_inbox_binding_rejects_source_archive_and_retained_drift(self) -> None:
        source_binding = parse_explicit_source(f"7:100:200:{'a' * 64}")
        retained_body = "retained\n"
        retained_binding = parse_retained_replacement(
            f"8:300:400:{'b' * 64}:{len(retained_body.encode())}:{hashlib.sha256(retained_body.encode()).hexdigest()}"
        )
        source = MailRecord(
            "7", "", "Agent <agent@example.test>", "Human <human@example.test>", "source", "source", "", "100", "200", raw_sha256="a" * 64
        )
        retained = MailRecord("8", "", "Agent", "Human", "retained", "retained", retained_body, "300", "400", raw_sha256="b" * 64)
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.fetch_final_gate_records", return_value=[retained]) as fetch_mock,
        ):
            self.assertFalse(
                final_inbox_bindings_intact(
                    client, "1", [source_binding], [retained_binding], "agent@example.test", "human@example.test"
                )
            )
        fetch_mock.assert_called_once()
        retained_drifts = (
            replace(retained, gmail_msgid="301"),
            replace(retained, gmail_thrid="401"),
            replace(retained, raw_sha256="c" * 64),
            replace(retained, body=f"{retained_body}x"),
            replace(retained, body=retained_body.upper()),
        )
        for changed in retained_drifts:
            with (
                self.subTest(changed=changed),
                patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
                patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
                patch("omo_manager.omo_manager_mail_compress.fetch_final_gate_records", return_value=[source, changed]),
            ):
                self.assertFalse(
                    final_inbox_bindings_intact(
                        client, "1", [source_binding], [retained_binding], "agent@example.test", "human@example.test"
                    )
                )

    def test_strict_fresh_source_location_gate_requires_exact_inbox_set_before_move(self) -> None:
        first = MailRecord("7", "", "Agent", "Human", "source-a", "a", gmail_msgid="100", gmail_thrid="200", raw_sha256="a" * 64)
        second = MailRecord("9", "", "Agent", "Human", "source-b", "b", gmail_msgid="101", gmail_thrid="200", raw_sha256="b" * 64)
        first_trash = replace(first, uid="70")
        second_trash = replace(second, uid="90")
        extra = replace(second, uid="11", gmail_msgid="102")
        sources = [parse_explicit_source(f"7:100:200:{'a' * 64}"), parse_explicit_source(f"9:101:200:{'b' * 64}")]
        cases = (
            ("exact", [first, second], [], True),
            ("first-already-trash", [second], [first_trash], False),
            ("second-already-trash", [first], [second_trash], False),
            ("both-already-trash", [], [first_trash, second_trash], False),
            ("wrong-inbox-uid", [replace(first, uid="8"), second], [], False),
            ("extra-inbox-selection", [first, second, extra], [], False),
        )
        for name, inbox, trash, intact in cases:
            with self.subTest(name=name):
                self.assertEqual(intact, strict_fresh_source_locations_intact(sources, inbox, trash))
                args = argparse.Namespace(
                    yes=True,
                    source=[f"7:100:200:{'a' * 64}", f"9:101:200:{'b' * 64}"],
                    context=[f"100:200:{'a' * 64}", f"101:200:{'b' * 64}"],
                    source_location_mode="strict-fresh",
                    replacement_id=["<replacement@example.test>"],
                    retained_replacement=[TEST_RETAINED_REPLACEMENT],
                    task_id=["task-a"],
                    preparer="owner-a",
                    reviewer="reviewer-b",
                    task_source=["1:100", "1:101"],
                    route_resolution=[],
                    source_uidvalidity="1",
                )
                client = FakeClient({})
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                    patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
                    patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
                    patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
                    patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=(inbox, trash)),
                    patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=False) as replacement_mock,
                    patch("omo_manager.omo_manager_mail_compress.imap_uid") as move_mock,
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(1, cmd_trash_explicit(args))
                self.assertEqual(1 if intact else 0, replacement_mock.call_count)
                move_mock.assert_not_called()
                self.assertIn("source_location_mode=strict-fresh", output.getvalue())

    def test_final_gate_fetch_combines_full_content_and_gmail_identity(self) -> None:
        raw = (
            b"From: Agent <agent@example.test>\r\n"
            b"To: Human <human@example.test>\r\n"
            b"Subject: retained\r\n"
            b"Message-ID: <retained@example.test>\r\n\r\nbody\r\n"
        )
        client = FakeClient(
            {
                ("fetch", "7", FINAL_GATE_FETCH): (
                    "OK",
                    [
                        (b"1 (UID 7 FLAGS () X-GM-MSGID 300 X-GM-THRID 400 X-GM-LABELS (\\Inbox) BODY[] {120}", raw),
                        b")",
                    ],
                )
            }
        )
        records = fetch_final_gate_records(client, ["7"])
        self.assertEqual(1, len(records))
        self.assertEqual(
            ("7", "300", "400", hashlib.sha256(raw).hexdigest()),
            (records[0].uid, records[0].gmail_msgid, records[0].gmail_thrid, records[0].raw_sha256),
        )

    def test_final_gate_fetch_associates_metadata_trailer_after_literal(self) -> None:
        raw = (
            b"From: Agent <agent@example.test>\r\n"
            b"To: Human <human@example.test>\r\n"
            b"Subject: retained\r\n"
            b"Message-ID: <retained@example.test>\r\n\r\nbody\r\n"
        )
        client = FakeClient(
            {
                ("fetch", "7", FINAL_GATE_FETCH): (
                    "OK",
                    [
                        (b"1 (UID 7 BODY[] {120}", raw),
                        b" FLAGS () X-GM-MSGID 300 X-GM-THRID 400 X-GM-LABELS (\\Inbox))",
                    ],
                )
            }
        )
        records = fetch_final_gate_records(client, ["7"])
        self.assertEqual(
            ("7", "300", "400", hashlib.sha256(raw).hexdigest()),
            (records[0].uid, records[0].gmail_msgid, records[0].gmail_thrid, records[0].raw_sha256),
        )

    def test_final_gate_fetch_rejects_malformed_duplicate_ambiguous_and_mismatched_records(self) -> None:
        raw = b"From: Agent <agent@example.test>\r\nTo: Human <human@example.test>\r\n\r\nbody\r\n"
        responses = (
            [(b"1 (UID 7 BODY[] {80}", raw), b")"],
            [
                (b"1 (UID 7 X-GM-MSGID 300 X-GM-THRID 400 BODY[] {80}", raw),
                b")",
                (b"2 (UID 7 X-GM-MSGID 300 X-GM-THRID 400 BODY[] {80}", raw),
                b")",
            ],
            [(b"1 (UID 7 UID 8 X-GM-MSGID 300 X-GM-THRID 400 BODY[] {80}", raw), b")"],
            [(b"1 (UID 8 X-GM-MSGID 300 X-GM-THRID 400 BODY[] {80}", raw), b")"],
        )
        for response in responses:
            with self.subTest(response=response):
                client = FakeClient({("fetch", "7", FINAL_GATE_FETCH): ("OK", response)})
                with self.assertRaises(RuntimeError):
                    fetch_final_gate_records(client, ["7"])

    def test_retained_replacement_contract_accepts_guest_and_teamtype_bindings(self) -> None:
        bindings = (
            (
                "18692:1874858195211945940:1874832269607972216:"
                "796701fadec87d3e2b2399a6f50a49d98274259d63a2b73b11e437082742829f:3708:"
                "d1ecf9c36d114f29b7ea837d967f101cac2838c121314318b9a1bd0effa7c897",
                ("18692", "1874858195211945940", "1874832269607972216", 3708),
            ),
            (
                "18693:1874860920982136423:1874844935645274852:"
                "753ecf258f29155a4deb851076cc5f67085c921a4237af89f3a8038c83e21969:7627:"
                "c921eb8b19440fe2a473498db2621976d887346ecf0ba8448e028cbc9ba0722a",
                ("18693", "1874860920982136423", "1874844935645274852", 7627),
            ),
        )
        for value, expected in bindings:
            with self.subTest(uid=expected[0]):
                binding = parse_retained_replacement(value)
                self.assertEqual(expected, (binding.uid, binding.gmail_msgid, binding.gmail_thrid, binding.body_bytes))

    def test_retained_replacement_gate_rejects_every_exact_binding_drift(self) -> None:
        body = "reviewed replacement\n"
        binding = parse_retained_replacement(
            f"18692:300:400:{'a' * 64}:{len(body.encode())}:{hashlib.sha256(body.encode()).hexdigest()}"
        )
        exact = MailRecord(
            binding.uid,
            "",
            "Agent",
            "Human",
            "replacement",
            "msg",
            body,
            binding.gmail_msgid,
            binding.gmail_thrid,
            raw_sha256=binding.raw_sha256,
        )
        drifts = {
            "uid-absence": ([], [exact], binding),
            "gmail-message": ([binding.uid], [replace(exact, gmail_msgid="301")], binding),
            "gmail-thread": ([binding.uid], [replace(exact, gmail_thrid="401")], binding),
            "raw-digest": ([binding.uid], [replace(exact, raw_sha256="b" * 64)], binding),
            "body-size": ([binding.uid], [exact], replace(binding, body_bytes=binding.body_bytes + 1)),
            "body-digest": ([binding.uid], [exact], replace(binding, body_sha256="b" * 64)),
        }
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=[binding.uid]),
            patch("omo_manager.omo_manager_mail_compress.fetch_records", return_value=[exact]),
        ):
            self.assertTrue(retained_replacements_intact(client, [binding]))
        for name, (uids, records, changed_binding) in drifts.items():
            with (
                self.subTest(name=name),
                patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=uids),
                patch("omo_manager.omo_manager_mail_compress.fetch_records", return_value=records),
            ):
                self.assertFalse(retained_replacements_intact(client, [changed_binding]))

    def test_trash_explicit_retained_replacement_gate_succeeds_strictly(self) -> None:
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] source",
            "source",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256="a" * 64,
        )
        body = "reviewed replacement\n"
        retained_value = f"18692:300:400:{'b' * 64}:{len(body.encode())}:{hashlib.sha256(body.encode()).hexdigest()}"
        retained = MailRecord(
            "18692",
            "",
            "Agent",
            "Human",
            "[worker:0] replacement",
            "retained",
            body,
            "300",
            "400",
            raw_sha256="b" * 64,
        )
        trashed = replace(source, uid="70")
        args = argparse.Namespace(
            yes=True,
            source=[f"7:100:200:{'a' * 64}"],
            context=[f"100:200:{'a' * 64}"],
            replacement_id=["<replacement@example.test>"],
            retained_replacement=[retained_value],
            task_id=["task-a"],
            preparer="owner-a",
            reviewer="reviewer-b",
            task_source=["1:100"],
            route_resolution=[],
            source_uidvalidity="1",
        )
        client = FakeClient({})
        final_context_client = FakeClient({})

        def retained_subset(_client: FakeClient, uids: list[str]) -> list[str]:
            return list(uids)

        def context_gate(*_args: object, observed: object = None, **_kwargs: object) -> bool:
            if callable(observed):
                observed("contexts-trash")
                observed("contexts-all")
            return True

        def inbox_gate(*_args: object, observed: object = None, **_kwargs: object) -> bool:
            assert callable(observed)
            observed("inbox-bindings")
            return True

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=[(client, {}), (final_context_client, {})]),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch(
                "omo_manager.omo_manager_mail_compress.special_use_mailboxes",
                return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"},
            ),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], []), ([], [trashed])]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="300"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", side_effect=context_gate),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", side_effect=inbox_gate),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", side_effect=retained_subset),
            patch("omo_manager.omo_manager_mail_compress.fetch_records", return_value=[retained]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", return_value=("OK", [b""])) as move_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(0, cmd_trash_explicit(args))
        self.assertEqual(1, move_mock.call_count)
        self.assertIn("retained_replacements=1", output.getvalue())
        self.assertIn("final_gate_passed=1", output.getvalue())
        self.assertIn("final_gate_observations=boundary>uidvalidity>special-use>contexts-trash>contexts-all>inbox-bindings", output.getvalue())
        self.assertIn("post_move_reconciled=1", output.getvalue())

    def test_trash_explicit_retained_replacement_drift_fails_before_move(self) -> None:
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] source",
            "source",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256="a" * 64,
        )
        body = "reviewed replacement\n"
        body_sha256 = hashlib.sha256(body.encode()).hexdigest()
        retained_value = f"18692:300:400:{'b' * 64}:{len(body.encode())}:{body_sha256}"
        exact = MailRecord(
            "18692",
            "",
            "Agent",
            "Human",
            "[worker:0] replacement",
            "retained",
            body,
            "300",
            "400",
            raw_sha256="b" * 64,
        )
        cases = {
            "source-archive": (exact, False, True, None),
            "uid-absence": (exact, True, False, None),
            "uidvalidity": (exact, False, False, ["1", "1", "1", "2"]),
            "gmail-message": (replace(exact, gmail_msgid="301"), False, False, None),
            "gmail-thread": (replace(exact, gmail_thrid="401"), False, False, None),
            "raw-digest": (replace(exact, raw_sha256="c" * 64), False, False, None),
            "body-size": (replace(exact, body=f"{body}x"), False, False, None),
            "body-digest": (replace(exact, body=body.upper()), False, False, None),
        }
        for name, (retained, absent, archived, uidvalidities) in cases.items():
            with self.subTest(name=name):
                args = argparse.Namespace(
                    yes=True,
                    source=[f"7:100:200:{'a' * 64}"],
                    context=[f"100:200:{'a' * 64}"],
                    replacement_id=["<replacement@example.test>"],
                    retained_replacement=[retained_value],
                    task_id=["task-a"],
                    preparer="owner-a",
                    reviewer="reviewer-b",
                    task_source=["1:100"],
                    route_resolution=[],
                    source_uidvalidity="1",
                )
                client = FakeClient({})
                final_context_client = FakeClient({})
                gate_events: list[str] = []

                def retained_subset(_client: FakeClient, uids: list[str]) -> list[str]:
                    if uids == ["7"]:
                        return list(uids)
                    return [uid for uid in uids if not (absent and uid == "18692") and not (archived and uid == "7")]

                def final_records(_client: FakeClient, _uids: list[str]) -> list[MailRecord]:
                    return [record for record in (source, retained) if not (absent and record.uid == "18692") and not (archived and record.uid == "7")]

                def context_gate(*_args: object, observed: object = None, **_kwargs: object) -> bool:
                    assert callable(observed)
                    observed("contexts-trash")
                    observed("contexts-all")
                    return True

                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=[(client, {}), (final_context_client, {})]),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                    patch(
                        "omo_manager.omo_manager_mail_compress.selected_uidvalidity",
                        side_effect=uidvalidities if uidvalidities is not None else None,
                        return_value="1",
                    ),
                    patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
                    patch(
                        "omo_manager.omo_manager_mail_compress.special_use_mailboxes",
                        return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"},
                    ),
                    patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], [])]),
                    patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
                    patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="300"),
                    patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
                    patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
                    patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True),
                    patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", side_effect=context_gate),
                    patch("omo_manager.omo_manager_mail_compress.final_gate_event", side_effect=gate_events.append),
                    patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
                    patch("omo_manager.omo_manager_mail_compress.inbox_subset", side_effect=retained_subset),
                    patch("omo_manager.omo_manager_mail_compress.fetch_final_gate_records", side_effect=final_records),
                    patch("omo_manager.omo_manager_mail_compress.imap_uid") as move_mock,
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(1, cmd_trash_explicit(args))
                move_mock.assert_not_called()
                if name != "uidvalidity":
                    self.assertIn("contexts-all", gate_events)
                self.assertIn("final_gate_passed=0", output.getvalue())
                self.assertIn("move_attempted=0", output.getvalue())

    def test_trash_explicit_rejects_missing_conflicting_or_wrong_sender_target(self) -> None:
        raw_sha256 = "a" * 64
        other_sha256 = "b" * 64
        cases = (
            ("missing", "subject", [], "[worker:0] replacement", []),
            ("conflicting", "[worker:0] subject", [MailRecord("8", "", "Agent", "Human", "[other:1] prior", "msg-b", gmail_msgid="101", gmail_thrid="200", raw_sha256=other_sha256)], "[worker:0] replacement", []),
            ("wrong-replacement", "[worker:0] subject", [], "[other:1] replacement", []),
            ("wrong-resolution-target", "[worker:0] subject", [], "[worker:0] replacement", ["task-a=other:1"]),
        )
        for name, source_subject, extra_context, replacement_subject_value, route_resolution in cases:
            with self.subTest(name=name):
                source = MailRecord(
                    "7",
                    "",
                    "Agent <agent@example.test>",
                    "Human <human@example.test>",
                    source_subject,
                    "msg-a",
                    gmail_msgid="100",
                    gmail_thrid="200",
                    raw_sha256=raw_sha256,
                )
                context_args = [f"100:200:{raw_sha256}", *[f"{record.gmail_msgid}:200:{record.raw_sha256}" for record in extra_context]]
                args = type(
                    "DirectArgs",
                    (),
                    {
                        "yes": True,
                        "source": [f"7:100:200:{raw_sha256}"],
                        "context": context_args,
                        "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                        "task_id": "task-a",
                        "preparer": "owner-a",
                        "reviewer": "reviewer-b",
                        "task_source": ["1:100"],
                        "route_resolution": route_resolution,
                        "source_uidvalidity": "1",
                    },
                )()
                client = FakeClient({})
                with (
                    patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
                    patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
                    patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
                    patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
                    patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
                    patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
                    patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
                    patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
                    patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value=replacement_subject_value),
                    patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source, *extra_context]}),
                    patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
                ):
                    self.assertEqual(1, cmd_trash_explicit(args))
                imap_uid_mock.assert_not_called()

    def test_trash_explicit_moves_only_after_live_revalidation_without_writes(self) -> None:
        raw_sha256 = "a" * 64
        prior_sha256 = "b" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        prior = MailRecord("8", "", "Agent", "Human", "[legacy:2] prior", "msg-b", gmail_msgid="101", gmail_thrid="200", raw_sha256=prior_sha256)
        second = replace(source, uid="9", gmail_msgid="102", gmail_thrid="201", raw_sha256="c" * 64)
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}", f"9:102:201:{'c' * 64}"],
                "context": [f"100:200:{raw_sha256}", f"101:200:{prior_sha256}", f"102:201:{'c' * 64}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100", "1:102"],
                "route_resolution": ["task-a=wl:31.0"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})
        final_context_client = FakeClient({})
        trashed = [replace(source, uid="70"), replace(second, uid="90")]
        events: list[tuple[str, ...]] = []
        context_clients: list[FakeClient] = []

        def selected_uidvalidity(_client: FakeClient) -> str:
            events.append(("uidvalidity",))
            return "1"

        def select_mailbox(_client: FakeClient, mailbox: str, readonly: bool) -> None:
            events.append(("select", mailbox, str(readonly)))

        def move(*_args: object) -> tuple[str, list[bytes]]:
            events.append(("move",))
            return "OK", [b""]

        def context_intact(context_client: FakeClient, *_args: object, allow_additive: bool, **_kwargs: object) -> bool:
            context_clients.append(context_client)
            events.append(("context", str(allow_additive)))
            return True

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=[(client, {}), (final_context_client, {})]) as open_mock,
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", side_effect=selected_uidvalidity),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source, second], []), ([source, second], []), ([], trashed)]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", side_effect=[True, True]),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[wl:31] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source, prior], "201": [second]}),
            patch(
                "omo_manager.omo_manager_mail_compress.special_use_mailboxes",
                return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"},
            ),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=context_intact) as context_mock,
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True) as contexts_mock,
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox", side_effect=select_mailbox),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", side_effect=[["7", "9"], ["7", "9"]]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", side_effect=move) as imap_uid_mock,
            patch("omo_manager.omo_manager_mail_compress.write_private_exclusive") as write_mock,
        ):
            self.assertEqual(0, cmd_trash_explicit(args))
        self.assertIn((client, "move-explicit-sources-to-trash", "MOVE", "7,9", '"[Gmail]/Trash"'), [call.args for call in imap_uid_mock.call_args_list])
        self.assertEqual([False] * 2, [call.kwargs["allow_additive"] for call in context_mock.call_args_list])
        self.assertEqual([client, client], context_clients)
        self.assertEqual(2, contexts_mock.call_count)
        self.assertEqual([False, True], [call.kwargs["readonly"] for call in open_mock.call_args_list])
        move_index = events.index(("move",))
        self.assertEqual([("select", "INBOX", "False"), ("uidvalidity",)], events[:2])
        self.assertEqual(("uidvalidity",), events[move_index - 1])
        self.assertFalse(any("EXPUNGE" in call.args for call in imap_uid_mock.call_args_list))
        write_mock.assert_not_called()

    def test_trash_explicit_additive_compatibility_mode_preserves_move_safeguards(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = argparse.Namespace(
            yes=True,
            source=[f"7:100:200:{raw_sha256}"],
            context=[f"100:200:{raw_sha256}"],
            replacement_id=[],
            replacement_not_required=True,
            human_approval_file=Path("/approval"),
            human_approval_quote=SOURCE_1140_APPROVAL_QUOTE,
            independent_review_file=Path("/review"),
            task_id=["task-a"],
            preparer="owner-a",
            reviewer="reviewer-b",
            task_source=["1:100"],
            route_resolution=[],
            source_uidvalidity="1",
            allow_additive_final_context=True,
        )
        client = FakeClient({})
        trashed = replace(source, uid="70")
        context_calls: list[bool] = []

        def additive_arrival_gate(
            _client: FakeClient,
            _all_mailbox: str,
            _records: list[object],
            *,
            allow_additive: bool,
            **_kwargs: object,
        ) -> bool:
            context_calls.append(allow_additive)
            return len(context_calls) == 1 or allow_additive

        with (
            patch("omo_manager.omo_manager_mail_compress.require_source_1140_direct_removal"),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], []), ([], [trashed])]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists") as replacement_exists_mock,
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid") as replacement_gmail_mock,
            patch("omo_manager.omo_manager_mail_compress.replacement_subject") as replacement_subject_mock,
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch(
                "omo_manager.omo_manager_mail_compress.special_use_mailboxes",
                return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"},
            ),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=additive_arrival_gate),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", side_effect=[["7"], ["7"]]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", return_value=("OK", [b""])) as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(0, cmd_trash_explicit(args))
        self.assertIn((client, "move-explicit-sources-to-trash", "MOVE", "7", '"[Gmail]/Trash"'), [call.args for call in imap_uid_mock.call_args_list])
        self.assertEqual([False], context_calls)
        self.assertIn("final_context=additive-compatible", output.getvalue())
        replacement_exists_mock.assert_not_called()
        replacement_gmail_mock.assert_not_called()
        replacement_subject_mock.assert_not_called()
        self.assertFalse(any("EXPUNGE" in call.args for call in imap_uid_mock.call_args_list))

    def test_trash_explicit_retries_verified_trash_without_another_move(self) -> None:
        raw_sha256 = "a" * 64
        trashed = MailRecord(
            "70",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
                "source_location_mode": "recover-partial-move",
            },
        )()
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([], [trashed])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [trashed]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(0, cmd_trash_explicit(args))
        imap_uid_mock.assert_not_called()
        self.assertIn("source_location_mode=recover-partial-move", output.getvalue())
        self.assertIn("move_outcome=not-needed", output.getvalue())

    def test_trash_explicit_post_move_verification_failure_returns_terminal_summary(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch(
                "omo_manager.omo_manager_mail_compress.observe_explicit_sources",
                side_effect=[
                    ([source], []),
                    ([source], []),
                    ImapOperationError("gmail-message-search message=100", "IMAP operation timed out"),
                ],
            ),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=[True, True]) as context_mock,
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", return_value=("OK", [b""])) as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertIn("move_attempted=1", output.getvalue())
        self.assertIn("moved_now=0", output.getvalue())
        self.assertIn("post_move_verified=0", output.getvalue())
        self.assertIn("post_move_verification_error=gmail-message-search_message=100", output.getvalue())
        self.assertEqual(1, context_mock.call_count)
        self.assertEqual(1, imap_uid_mock.call_count)

    def test_trash_explicit_move_timeout_returns_unknown_outcome_summary(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], [])]) as observe_mock,
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=[True, True, True]),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch(
                "omo_manager.omo_manager_mail_compress.imap_uid",
                side_effect=ImapOperationError("move-explicit-sources-to-trash", "IMAP operation timed out"),
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertEqual(2, observe_mock.call_count)
        self.assertIn("move_attempted=1", output.getvalue())
        self.assertIn("moved_now=0", output.getvalue())
        self.assertIn("move_outcome=unknown", output.getvalue())
        self.assertIn("post_move_verified=0", output.getvalue())
        self.assertIn("post_move_verification_error=move-explicit-sources-to-trash", output.getvalue())

    def test_trash_explicit_pre_move_imap_failure_returns_terminal_summary(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch(
                "omo_manager.omo_manager_mail_compress.replacement_gmail_msgid",
                side_effect=ImapOperationError("replacement-gmail-identity-search", "IMAP operation timed out"),
            ),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        imap_uid_mock.assert_not_called()
        self.assertIn("move_attempted=0", output.getvalue())
        self.assertIn("move_outcome=not-attempted", output.getvalue())
        self.assertIn("post_move_verified=0", output.getvalue())
        self.assertIn("post_move_verification_error=replacement-gmail-identity-search", output.getvalue())

    def test_trash_explicit_pre_move_runtime_timeout_returns_terminal_summary(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        def slow_context(*_args: object, **_kwargs: object) -> bool:
            time.sleep(1)
            return True

        with (
            patch("omo_manager.omo_manager_mail_compress.TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S", 0.01),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=slow_context),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        imap_uid_mock.assert_not_called()
        self.assertTrue(getattr(client, "_omo_operation_timed_out", False))
        self.assertIn("move_attempted=0", output.getvalue())
        self.assertIn("move_outcome=not-attempted", output.getvalue())
        self.assertIn("post_move_verified=0", output.getvalue())
        self.assertIn("post_move_verification_error=direct-context-intact", output.getvalue())

    def test_trash_explicit_late_pre_move_timeout_reports_no_move_attempt(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        replacement_calls = 0

        def replacement_exists(*_args: object, **_kwargs: object) -> bool:
            nonlocal replacement_calls
            replacement_calls += 1
            if replacement_calls == 2:
                time.sleep(1)
            return True

        with (
            patch("omo_manager.omo_manager_mail_compress.TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S", 0.01),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", side_effect=replacement_exists),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        imap_uid_mock.assert_not_called()
        self.assertIn("move_attempted=0", output.getvalue())
        self.assertIn("move_outcome=not-attempted", output.getvalue())
        self.assertIn("post_move_verification_error=replacement-exists-final", output.getvalue())

    def test_trash_explicit_refuses_when_process_alarm_is_active(self) -> None:
        raw_sha256 = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        old_handler = signal.getsignal(signal.SIGALRM)
        try:
            signal.signal(signal.SIGALRM, lambda _signum, _frame: None)
            signal.setitimer(signal.ITIMER_REAL, 30)
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mock,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(1, cmd_trash_explicit(args))
            open_mock.assert_not_called()
            self.assertIn("move_attempted=0", output.getvalue())
            self.assertIn("move_outcome=not-attempted", output.getvalue())
            self.assertIn("post_move_verification_error=arm-pre-move-timer", output.getvalue())
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    def test_trash_explicit_open_mailbox_timeout_closes_late_connect_client(self) -> None:
        raw_sha256 = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        clients: list[object] = []

        class LateClient:
            def __init__(self, _host: str, timeout: float) -> None:
                time.sleep(0.05)
                self.timeout = timeout
                self.shutdown_calls = 0
                clients.append(self)

            def shutdown(self) -> None:
                self.shutdown_calls += 1

        with (
            patch("omo_manager.omo_manager_mail_compress.TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S", 0.01),
            patch("omo_manager.omo_manager_mail_compress.load_config", return_value={"host": "imap.example.test", "user": "u", "password": "p"}),
            patch("omo_manager.omo_manager_mail_compress.imaplib.IMAP4_SSL", LateClient),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not clients:
            time.sleep(0.01)
        self.assertEqual(1, len(clients))
        self.assertEqual(1, getattr(clients[0], "shutdown_calls"))
        self.assertIn("move_attempted=0", output.getvalue())
        self.assertIn("post_move_verification_error=open-mailbox", output.getvalue())

    def test_trash_explicit_uidvalidity_refusal_returns_terminal_summary(self) -> None:
        raw_sha256 = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "2",
            },
        )()
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as error,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertIn("refusing because inspected INBOX UIDVALIDITY changed", error.getvalue())
        self.assertIn("move_attempted=0", output.getvalue())
        self.assertIn("move_outcome=not-attempted", output.getvalue())
        self.assertIn("post_move_verification_error=selected-uidvalidity", output.getvalue())

    def test_trash_explicit_timer_setup_failure_restores_signal_handler(self) -> None:
        raw_sha256 = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        old_handler = signal.getsignal(signal.SIGALRM)
        marker = object()

        def handler(_signum: int, _frame: object) -> None:
            return None

        def fail_setitimer(*_args: object) -> tuple[float, float]:
            raise RuntimeError("setitimer failed")

        try:
            signal.signal(signal.SIGALRM, handler)
            with (
                patch("omo_manager.omo_manager_mail_compress.signal.setitimer", side_effect=fail_setitimer),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mock,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(1, cmd_trash_explicit(args))
            open_mock.assert_not_called()
            self.assertIs(handler, signal.getsignal(signal.SIGALRM))
            self.assertIsNot(marker, signal.getsignal(signal.SIGALRM))
            self.assertIn("post_move_verification_error=arm-pre-move-timer", output.getvalue())
        finally:
            signal.signal(signal.SIGALRM, old_handler)

    def test_trash_explicit_timer_disarm_failure_restores_handler_and_retries(self) -> None:
        from omo_manager import omo_manager_mail_compress as mail_compress

        old_handler = signal.getsignal(signal.SIGALRM)
        calls = 0

        def original_handler(_signum: int, _frame: object) -> None:
            return None

        real_setitimer = signal.setitimer

        def flaky_setitimer(*args: object) -> tuple[float, float]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("disarm failed")
            return real_setitimer(*args)

        try:
            signal.signal(signal.SIGALRM, original_handler)
            with patch("omo_manager.omo_manager_mail_compress.signal.setitimer", side_effect=flaky_setitimer):
                disarm = mail_compress.arm_trash_explicit_pre_move_timer(lambda: "stage-a", lambda: None)
                with self.assertRaisesRegex(RuntimeError, "disarm failed"):
                    disarm()
                self.assertIs(original_handler, signal.getsignal(signal.SIGALRM))
                disarm()
            self.assertIs(original_handler, signal.getsignal(signal.SIGALRM))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old_handler)

    def test_trash_explicit_refuses_timer_from_non_main_thread(self) -> None:
        raw_sha256 = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        output = io.StringIO()
        result: list[int] = []

        def run() -> None:
            with (
                patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mock,
                redirect_stdout(output),
            ):
                result.append(cmd_trash_explicit(args))
                open_mock.assert_not_called()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual([1], result)
        self.assertIn("post_move_verification_error=arm-pre-move-timer", output.getvalue())

    def test_trash_explicit_replacement_subject_timeout_aborts_client(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        def slow_subject(*_args: object, **_kwargs: object) -> str:
            time.sleep(1)
            return "[worker:0] replacement"

        with (
            patch("omo_manager.omo_manager_mail_compress.TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S", 0.01),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", side_effect=slow_subject),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        imap_uid_mock.assert_not_called()
        self.assertTrue(getattr(client, "_omo_operation_timed_out", False))
        self.assertIn("move_attempted=0", output.getvalue())
        self.assertIn("post_move_verification_error=replacement-subject", output.getvalue())

    def test_trash_explicit_replacement_subject_timeout_skips_restore_select(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})
        select_log: list[tuple[str, bool]] = []

        def select_mailbox(_client: FakeClient, mailbox: str, readonly: bool) -> None:
            select_log.append((mailbox, readonly))

        def timed_out_subject_search(*_args: object, **_kwargs: object) -> tuple[str, list[bytes]]:
            time.sleep(1)
            return "OK", [b"1"]

        with (
            patch("omo_manager.omo_manager_mail_compress.TRASH_EXPLICIT_PRE_MOVE_TIMEOUT_S", 0.01),
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox", side_effect=select_mailbox),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", side_effect=timed_out_subject_search) as imap_uid_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        imap_uid_mock.assert_called_once()
        self.assertTrue(getattr(client, "_omo_operation_timed_out", False))
        self.assertEqual(("[Gmail]/All Mail", True), select_log[-1])
        self.assertIn("post_move_verification_error=replacement-subject", output.getvalue())

    def test_trash_explicit_non_ok_move_verifies_sources_and_returns_summary(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        trashed = replace(source, uid="70")
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], []), ([], [trashed])]) as observe_mock,
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=[True, True, True]),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", return_value=("NO", [b"partial move"])),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertEqual(3, observe_mock.call_count)
        self.assertIn("move_attempted=1", output.getvalue())
        self.assertIn("moved_now=1", output.getvalue())
        self.assertIn("move_outcome=failed", output.getvalue())
        self.assertIn("verified_trash=1", output.getvalue())
        self.assertIn("post_move_verified=0", output.getvalue())
        self.assertIn("post_move_reconciliation_ran=1", output.getvalue())
        self.assertIn("post_move_reconciled=1", output.getvalue())
        self.assertIn("post_move_verification_error=move-explicit-sources-to-trash:NO", output.getvalue())

    def test_trash_explicit_post_move_logout_failure_returns_terminal_summary(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        trashed = replace(source, uid="70")
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], []), ([], [trashed])]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", side_effect=[True, True, True]),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.final_inbox_bindings_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid", return_value=("OK", [b""])),
            patch("omo_manager.omo_manager_mail_compress.logout_mailbox", side_effect=RuntimeError("logout failed")),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertIn("moved_now=1", output.getvalue())
        self.assertIn("verified_trash=1", output.getvalue())
        self.assertIn("post_move_verified=0", output.getvalue())
        self.assertIn("post_move_reconciliation_ran=1", output.getvalue())
        self.assertIn("post_move_reconciled=1", output.getvalue())
        self.assertIn("post_move_verification_error=logout:RuntimeError:logout_failed", output.getvalue())

    def test_trash_explicit_rechecks_replacement_sender_target_at_final_gate(self) -> None:
        raw_sha256 = "a" * 64
        prior_sha256 = "b" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] complete",
            "msg-a",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        prior = MailRecord("8", "", "Agent", "Human", "[legacy:2] prior", "msg-b", gmail_msgid="101", gmail_thrid="200", raw_sha256=prior_sha256)
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}", f"101:200:{prior_sha256}"],
                "replacement_id": "<replacement@example.test>",
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": "task-a",
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100"],
                "route_resolution": ["task-a=wl:31"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], [])]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", side_effect=["[wl:31] replacement", "[other:1] replacement"]) as subject_mock,
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source, prior]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertEqual(2, subject_mock.call_count)
        imap_uid_mock.assert_not_called()

    def test_trash_explicit_split_requires_one_unique_replacement_per_task(self) -> None:
        raw_sha256 = "a" * 64
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}"],
                "replacement_id": ["<replacement-a@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a", "task-b"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100", "2:100"],
                "source_uidvalidity": "1",
            },
        )()
        with patch("omo_manager.omo_manager_mail_compress.open_mailbox") as open_mailbox_mock:
            self.assertEqual(2, cmd_trash_explicit(args))
        open_mailbox_mock.assert_not_called()

    def test_trash_explicit_split_rechecks_every_replacement_at_final_gate(self) -> None:
        raw_sha256 = "a" * 64
        prior_sha256 = "b" * 64
        source = MailRecord(
            "7",
            "",
            "Agent <agent@example.test>",
            "Human <human@example.test>",
            "[worker:0] subject",
            "msg",
            gmail_msgid="100",
            gmail_thrid="200",
            raw_sha256=raw_sha256,
        )
        prior = MailRecord("8", "", "Agent", "Human", "[legacy:2] prior", "msg-b", gmail_msgid="101", gmail_thrid="200", raw_sha256=prior_sha256)
        args = type(
            "DirectArgs",
            (),
            {
                "yes": True,
                "source": [f"7:100:200:{raw_sha256}"],
                "context": [f"100:200:{raw_sha256}", f"101:200:{prior_sha256}"],
                "replacement_id": ["<replacement-a@example.test>", "<replacement-b@example.test>"],
                "retained_replacement": [TEST_RETAINED_REPLACEMENT_998, TEST_RETAINED_REPLACEMENT],
                "task_id": ["task-a", "task-b"],
                "preparer": "owner-a",
                "reviewer": "reviewer-b",
                "task_source": ["1:100", "2:100"],
                "route_resolution": ["task-a=worker:0", "task-b=other:1"],
                "source_uidvalidity": "1",
            },
        )()
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], [])]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", side_effect=[True, True, True, False]),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", side_effect=["998", "999"]),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", side_effect=["[worker:0] replacement a", "[other:1] replacement b"]),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source, prior]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True) as context_mock,
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        context_mock.assert_called_once()
        imap_uid_mock.assert_not_called()

    def test_trash_explicit_rejects_replacement_source_overlap(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord("7", "", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] subject", "msg", gmail_msgid="100", gmail_thrid="200", raw_sha256=raw_sha256)
        args = type("DirectArgs", (), {"yes": True, "source": [f"7:100:200:{raw_sha256}"], "context": [f"100:200:{raw_sha256}"], "replacement_id": "<replacement@example.test>", "retained_replacement": [TEST_RETAINED_REPLACEMENT], "task_id": "task:a", "preparer": "owner-a", "reviewer": "reviewer-b", "task_source": ["1:100"], "source_uidvalidity": "1"})()
        client = FakeClient({})
        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {})),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", return_value=([source], [])),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="100"),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        imap_uid_mock.assert_not_called()

    def test_trash_explicit_strict_final_gate_rejects_additive_arrival_without_move(self) -> None:
        raw_sha256 = "a" * 64
        source = MailRecord("7", "", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] subject", "msg", gmail_msgid="100", gmail_thrid="200", raw_sha256=raw_sha256)
        args = argparse.Namespace(
            yes=True,
            source=[f"7:100:200:{raw_sha256}"],
            context=[f"100:200:{raw_sha256}"],
            replacement_id="<replacement@example.test>",
            retained_replacement=[TEST_RETAINED_REPLACEMENT],
            task_id="task-a",
            preparer="owner-a",
            reviewer="reviewer-b",
            task_source=["1:100"],
            source_uidvalidity="1",
        )
        client = FakeClient({})
        final_context_client = FakeClient({})

        with (
            patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=[(client, {}), (final_context_client, {})]),
            patch("omo_manager.omo_manager_mail_compress.mail_boundary", return_value=("agent@example.test", "human@example.test")),
            patch("omo_manager.omo_manager_mail_compress.selected_uidvalidity", return_value="1"),
            patch("omo_manager.omo_manager_mail_compress.mailbox_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.special_use_mailboxes", return_value={r"\All": "[Gmail]/All Mail", r"\Sent": "[Gmail]/Sent Mail"}),
            patch("omo_manager.omo_manager_mail_compress.observe_explicit_sources", side_effect=[([source], []), ([source], [])]),
            patch("omo_manager.omo_manager_mail_compress.replacement_exists", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.replacement_gmail_msgid", return_value="999"),
            patch("omo_manager.omo_manager_mail_compress.replacement_subject", return_value="[worker:0] replacement"),
            patch("omo_manager.omo_manager_mail_compress.fetch_direct_thread_contexts", return_value={"200": [source]}),
            patch("omo_manager.omo_manager_mail_compress.direct_context_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.direct_contexts_intact", return_value=False),
            patch("omo_manager.omo_manager_mail_compress.retained_replacements_intact", return_value=True),
            patch("omo_manager.omo_manager_mail_compress.select_mailbox"),
            patch("omo_manager.omo_manager_mail_compress.inbox_subset", return_value=["7"]),
            patch("omo_manager.omo_manager_mail_compress.imap_uid") as imap_uid_mock,
            patch("omo_manager.omo_manager_mail_compress.write_private_exclusive") as write_mock,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(1, cmd_trash_explicit(args))
        self.assertIn("final_context=strict", output.getvalue())
        self.assertIn("final_gate_passed=0", output.getvalue())
        self.assertIn("post_move_reconciled=0", output.getvalue())
        self.assertIn("move_attempted=0", output.getvalue())
        imap_uid_mock.assert_not_called()
        write_mock.assert_not_called()

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

    def test_trash_superseded_human_approved_exact_removal_skips_thread_revalidation(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "7", GMAIL_METADATA_FETCH): [
                    self.gmail_metadata("7"),
                    self.gmail_metadata("7"),
                ],
                ("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""]),
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b"70"]),
                ("fetch", "70", FULL_FETCH): ("OK", [(b"message", raw)]),
                ("fetch", "70", GMAIL_METADATA_FETCH): self.gmail_metadata("70", labels=r"\Trash"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            approval_file = self.write_human_approval(Path(tmp))
            self.write_human_approval_scope(source_dir, [record], approval_file)
            approval_sha256 = hashlib.sha256(approval_file.read_bytes()).hexdigest()
            local_env = self.write_local_env_root(Path(tmp))
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", approval_sha256),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
                patch("omo_manager.omo_manager_mail_compress.reconciliation_thread_unchanged", side_effect=AssertionError("thread revalidation should be skipped")),
            ):
                self.assertEqual(0, cmd_trash_superseded(args))
            self.assertTrue((source_dir / "outcomes" / "200.tsv").exists())
        self.assertIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

    def test_trash_superseded_human_approved_exact_removal_requires_scope(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_rejects_ordinary_provenance(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            approval_file = self.write_human_approval(Path(tmp))
            ordinary_file = Path(tmp) / "ordinary.txt"
            ordinary_file.write_text("ordinary provenance\n", encoding="utf-8")
            ordinary_file.chmod(0o600)
            self.write_human_approval_scope(source_dir, [record], approval_file, provenance=str(ordinary_file.resolve()))
            approval_sha256 = hashlib.sha256(approval_file.read_bytes()).hexdigest()
            local_env = self.write_local_env_root(Path(tmp))
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", approval_sha256),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
            ):
                self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_rejects_multiple_sources(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        other_raw = self.raw_message("[worker:0] complete", "other").replace(b"<one@example.test>", b"<two@example.test>")
        first = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        second = MailRecord("8", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<two@example.test>").hexdigest()[:12], "other\n", "101", "200", "", r"\Inbox", hashlib.sha256(other_raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, first, [first, second])
            uid_file.write_text("7\n8\n", encoding="utf-8")
            approval_file = self.write_human_approval(Path(tmp))
            self.write_human_approval_scope(source_dir, [first, second], approval_file)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_rejects_replacement_id(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            approval_file = self.write_human_approval(Path(tmp))
            self.write_human_approval_scope(source_dir, [record], approval_file)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            args.replacement_id = "<replacement@example.test>"
            args.replacement_not_required = False
            self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_rejects_wrong_task_before_mailbox(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        self.assert_human_approved_exact_removal_rejected_before_mailbox(record, task_id="other-task")

    def test_trash_superseded_human_approved_exact_removal_rejects_wrong_quote_before_mailbox(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        self.assert_human_approved_exact_removal_rejected_before_mailbox(record, quote="Move something else")

    def test_trash_superseded_human_approved_exact_removal_rejects_wrong_hash_before_mailbox(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        self.assert_human_approved_exact_removal_rejected_before_mailbox(record, approval_sha256="0" * 64)

    def test_trash_superseded_human_approved_exact_removal_rejects_wrong_source_binding_before_mailbox(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        wrong_bindings = {
            "uid": f"8:{record.gmail_msgid}:{record.gmail_thrid}:{record.raw_sha256}",
            "gmail_msgid": f"{record.uid}:101:{record.gmail_thrid}:{record.raw_sha256}",
            "gmail_thrid": f"{record.uid}:{record.gmail_msgid}:201:{record.raw_sha256}",
            "raw_sha256": f"{record.uid}:{record.gmail_msgid}:{record.gmail_thrid}:{'0' * 64}",
        }
        for field, source_binding in wrong_bindings.items():
            with self.subTest(field=field):
                self.assert_human_approved_exact_removal_rejected_before_mailbox(record, source_binding=source_binding)

    def test_trash_superseded_human_approved_exact_removal_rejects_symlinked_manager_mail_root(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            actual = Path(tmp) / "actual_manager_mail"
            actual.mkdir(mode=0o700)
            (root / "manager_mail").symlink_to(actual, target_is_directory=True)
            approval_file = actual / SOURCE_815_APPROVAL_FILE
            approval_file.write_text(SOURCE_815_APPROVAL_QUOTE + "\n", encoding="utf-8")
            approval_file.chmod(0o600)
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            self.write_human_approval_scope(source_dir, [record], approval_file)
            local_env = self.write_local_env_root(root)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", hashlib.sha256(approval_file.read_bytes()).hexdigest()),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=AssertionError("mailbox must not open")),
            ):
                self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_rejects_env_root_alias(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            alias = Path(tmp) / "alias"
            root.mkdir()
            alias.symlink_to(root, target_is_directory=True)
            approval_file = self.write_human_approval(root)
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            self.write_human_approval_scope(source_dir, [record], approval_file)
            local_env = self.write_local_env_root(alias)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", hashlib.sha256(approval_file.read_bytes()).hexdigest()),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=AssertionError("mailbox must not open")),
            ):
                self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_ignores_env_root_override(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            trusted_root = Path(tmp) / "trusted"
            trusted_root.mkdir()
            attacker_root = Path(tmp) / "attacker"
            attacker_root.mkdir()
            approval_file = self.write_human_approval(attacker_root)
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            self.write_human_approval_scope(source_dir, [record], approval_file)
            local_env = self.write_local_env_root(trusted_root)
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch.dict(os.environ, {"OMO_WORK_LOGS_ROOT": str(attacker_root)}),
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", hashlib.sha256(approval_file.read_bytes()).hexdigest()),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", side_effect=AssertionError("mailbox must not open")),
            ):
                self.assertEqual(2, cmd_trash_superseded(args))

    def test_trash_superseded_human_approved_exact_removal_rejects_source_drift(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        changed_raw = self.raw_message("[worker:0] changed")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "UID", "7"): ("OK", [b"7"]),
                ("fetch", "7", FULL_FETCH): ("OK", [(b"message", changed_raw)]),
                ("fetch", "7", GMAIL_METADATA_FETCH): self.gmail_metadata("7"),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            approval_file = self.write_human_approval(Path(tmp))
            self.write_human_approval_scope(source_dir, [record], approval_file)
            approval_sha256 = hashlib.sha256(approval_file.read_bytes()).hexdigest()
            local_env = self.write_local_env_root(Path(tmp))
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", approval_sha256),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
                patch("omo_manager.omo_manager_mail_compress.reconciliation_thread_unchanged", side_effect=AssertionError("thread revalidation should be skipped")),
            ):
                self.assertEqual(1, cmd_trash_superseded(args))
            self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
        self.assertFalse(any(call[0].casefold() == "move" for call in client.uid_calls))

    def test_trash_superseded_human_approved_exact_removal_rejects_failed_trash_verification(self) -> None:
        raw = self.raw_message("[worker:0] complete")
        record = MailRecord("7", "date", "Agent <agent@example.test>", "Human <human@example.test>", "[worker:0] complete", hashlib.sha256(b"<one@example.test>").hexdigest()[:12], "body\n", "100", "200", "", r"\Inbox", hashlib.sha256(raw).hexdigest())
        client = FakeClient(
            {
                ("search", None, "UID", "7"): [("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b"7"]), ("OK", [b""])],
                ("fetch", "7", FULL_FETCH): [("OK", [(b"message", raw)]), ("OK", [(b"message", raw)])],
                ("fetch", "7", GMAIL_METADATA_FETCH): [
                    self.gmail_metadata("7"),
                    self.gmail_metadata("7"),
                ],
                ("MOVE", "7", '"[Gmail]/Trash"'): ("OK", [b""]),
                ("search", None, "X-GM-MSGID", "100"): ("OK", [b""]),
            },
            self.gmail_mailboxes(),
        )

        class Settings:
            agent_address = "agent@example.test"
            human_address = "human@example.test"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "export"
            uid_file = self.write_source_map(source_dir, record)
            approval_file = self.write_human_approval(Path(tmp))
            self.write_human_approval_scope(source_dir, [record], approval_file)
            approval_sha256 = hashlib.sha256(approval_file.read_bytes()).hexdigest()
            local_env = self.write_local_env_root(Path(tmp))
            args = Args(uid_file=uid_file, yes=True)
            args.task_id = SOURCE_815_TASK_ID
            args.human_approved_exact_removal = True
            args.human_approval_file = approval_file.resolve()
            args.human_approval_quote = SOURCE_815_APPROVAL_QUOTE
            with (
                patch("omo_manager.omo_manager_mail_compress.LOCAL_ENV_PATH", local_env),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_APPROVAL_SHA256", approval_sha256),
                patch("omo_manager.omo_manager_mail_compress.SOURCE_815_SOURCE_BINDING", self.source_815_test_binding(record)),
                patch("omo_manager.omo_manager_mail_compress.open_mailbox", return_value=(client, {"user": "human@example.test"})),
                patch("omo_manager.omo_manager_mail_compress.configured_agent_mail", return_value=Settings()),
                patch("omo_manager.omo_manager_mail_compress.reconciliation_thread_unchanged", side_effect=AssertionError("thread revalidation should be skipped")),
            ):
                self.assertEqual(1, cmd_trash_superseded(args))
            self.assertFalse((source_dir / "outcomes" / "200.tsv").exists())
        self.assertIn(("MOVE", "7", '"[Gmail]/Trash"'), client.uid_calls)

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
