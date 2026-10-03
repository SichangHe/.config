import json
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omo_manager.omo_completion_email import ordinary_sent_text
from omo_manager.omo_scorer_pending_reconcile import MANAGER, SOURCES, TASK, checked_spec, digest, reconcile, verify_mail


class ScorerPendingReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "manager_mail").mkdir()
        self.items = tuple(f"🧑 item {index}" for index in range(5))
        self.digest_patch = patch("omo_manager.omo_scorer_pending_reconcile.ITEM_DIGESTS", tuple(digest(item.encode()) for item in self.items))
        self.digest_patch.start()
        self.addCleanup(self.digest_patch.stop)
        self.pin_patch = patch(
            "omo_manager.omo_scorer_pending_reconcile.SOURCE_PINS",
            {identifier: (digest(f"Subject: test {identifier}\n\nrequest {identifier}".encode()), f"<human-{identifier}@example.test>") for identifier in SOURCES},
        )
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        self.task = self.root / TASK
        self.manager = self.root / MANAGER
        self.todo = self.root / "TODO.md"
        self.manager.write_text("---\nversion: v1.0.0\nstatus: long_running\nrunat: dw:65\ntool: codex\nmanagerat: dw:1\nis_manager: true\npending_task_items: []\n---\n", encoding="utf-8")
        self.todo.write_text(f"current:\n{MANAGER} dw:65\n{TASK} dw:2\nhuman pending:\nprevious:\n", encoding="utf-8")
        for identifier, source in SOURCES.items():
            (self.root / source).write_text(f"Subject: test {identifier}\n\nrequest {identifier}", encoding="utf-8")
        self.write_task()
        self.auth = patch("omo_manager.omo_scorer_pending_reconcile.current_active_task", return_value=self.manager)
        self.mail = patch("omo_manager.omo_scorer_pending_reconcile.verify_mail")
        self.auth.start()
        self.mail.start()
        self.addCleanup(self.auth.stop)
        self.addCleanup(self.mail.stop)

    def write_task(self, items: tuple[str, ...] | None = None) -> None:
        actual = self.items if items is None else items
        self.task.write_text(
            "---\nversion: v1.0.0\nstatus: running\nrunat: dw:2\ntool: codex\nmanagerat: dw:65\nis_manager: false\npending_task_items:\n"
            + "".join(f"  - {item}\n" for item in actual)
            + "---\n"
            + "".join(f"(record and delegate {source})\n" for source in SOURCES.values()),
            encoding="utf-8",
        )

    def spec(self, mode: str) -> dict[str, object]:
        scope = ("2172", "2245") if mode == "archive" else ("2175", "2176", "2229", "2233", "2245")
        return checked_spec(
            json.dumps(
                {
                    "mode": mode,
                    "nonce": ("1" if mode == "archive" else "2") * 64,
                    "task_sha256": digest(self.task.read_bytes()),
                    "manager_sha256": digest(self.manager.read_bytes()),
                    "todo_sha256": digest(self.todo.read_bytes()),
                    "sources": {identifier: {"sha256": digest((self.root / SOURCES[identifier]).read_bytes()), "message_id": f"<human-{identifier}@example.test>"} for identifier in scope},
                }
            ).encode()
        )

    def test_cancel_archive_then_complete_four_without_email(self) -> None:
        initial = self.spec("archive")
        original = self.task.read_bytes()
        preview = reconcile(self.root, initial, apply=False)
        self.assertEqual(self.task.read_bytes(), original)
        receipt = preview.split("receipt sha256=")[1].split(";")[0]
        with self.assertRaisesRegex(ValueError, "reviewed scorer receipt authority is absent"):
            reconcile(self.root, initial, apply=True, expected_receipt_sha256=receipt)
        with patch.dict("omo_manager.omo_scorer_pending_reconcile.APPROVED_RECEIPTS", {"archive": receipt}):
            self.assertIn("removed 1 items", reconcile(self.root, initial, apply=True, expected_receipt_sha256=receipt))
        remaining = self.task.read_text(encoding="utf-8")
        self.assertNotIn(self.items[0], remaining)
        self.assertTrue(all(item in remaining for item in self.items[1:]))
        with self.assertRaisesRegex(ValueError, "changed"):
            reconcile(self.root, initial, apply=True, expected_receipt_sha256=receipt)
        operational = self.spec("operational")
        preview = reconcile(self.root, operational, apply=False)
        receipt = preview.split("receipt sha256=")[1].split(";")[0]
        with patch.dict("omo_manager.omo_scorer_pending_reconcile.APPROVED_RECEIPTS", {"operational": receipt}):
            self.assertIn("removed 4 items", reconcile(self.root, operational, apply=True, expected_receipt_sha256=receipt))
        self.assertIn("pending_task_items: []", self.task.read_text(encoding="utf-8"))
        self.assertEqual(self.todo.read_text(encoding="utf-8"), f"current:\n{MANAGER} dw:65\n{TASK} dw:2\nhuman pending:\nprevious:\n")

    def test_wrong_receipt_todo_and_manager_fail_without_write(self) -> None:
        spec = self.spec("archive")
        original = self.task.read_bytes()
        with self.assertRaisesRegex(ValueError, "receipt approval"):
            reconcile(self.root, spec, apply=True, expected_receipt_sha256="0" * 64)
        self.assertEqual(original, self.task.read_bytes())
        self.todo.write_text(self.todo.read_text(encoding="utf-8") + "unrelated change\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed"):
            reconcile(self.root, spec, apply=False)
        self.assertEqual(original, self.task.read_bytes())
        self.todo.write_text(f"current:\n{MANAGER} dw:65\n{TASK} dw:2\nhuman pending:\nprevious:\n", encoding="utf-8")
        with patch("omo_manager.omo_scorer_pending_reconcile.current_active_task", side_effect=ValueError("detached owner")):
            with self.assertRaisesRegex(ValueError, "detached owner"):
                reconcile(self.root, spec, apply=False)
        self.assertEqual(original, self.task.read_bytes())

    def test_source_mismatch_and_mail_failure_do_not_modify_task(self) -> None:
        spec = self.spec("archive")
        original = self.task.read_bytes()
        (self.root / SOURCES["2245"]).write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Human source 2245 changed"):
            reconcile(self.root, spec, apply=False)
        (self.root / SOURCES["2245"]).write_text("Subject: test 2245\n\nrequest 2245", encoding="utf-8")
        with patch("omo_manager.omo_scorer_pending_reconcile.verify_mail", side_effect=ValueError("Sent missing")):
            with self.assertRaisesRegex(ValueError, "Sent missing"):
                reconcile(self.root, spec, apply=True, expected_receipt_sha256="0" * 64)
        self.assertEqual(original, self.task.read_bytes())

    def test_schema_rejects_ambiguous_authority(self) -> None:
        spec = self.spec("archive")
        spec["sources"]["2245"]["message_id"] = spec["sources"]["2172"]["message_id"]
        with self.assertRaisesRegex(ValueError, "reuses"):
            checked_spec(json.dumps(spec).encode())
        spec["sources"]["2245"]["message_id"] = "<human-2245@example.test>"
        spec["mode"] = "unknown"
        with self.assertRaisesRegex(ValueError, "source scope"):
            checked_spec(json.dumps(spec).encode())

    def test_reused_nonce_and_unpinned_source_fail_closed(self) -> None:
        spec = self.spec("archive")
        receipt = reconcile(self.root, spec, apply=False).split("receipt sha256=")[1].split(";")[0]
        with patch.dict("omo_manager.omo_scorer_pending_reconcile.APPROVED_RECEIPTS", {"archive": receipt}):
            reconcile(self.root, spec, apply=True, expected_receipt_sha256=receipt)
        next_spec = self.spec("operational")
        next_spec["nonce"] = spec["nonce"]
        with self.assertRaisesRegex(ValueError, "already consumed"):
            reconcile(self.root, next_spec, apply=False)
        next_spec["sources"]["2175"]["message_id"] = "<unreviewed@example.test>"
        with self.assertRaisesRegex(ValueError, "immutable Human source pins"):
            checked_spec(json.dumps(next_spec).encode())

    def test_operational_cross_thread_claim_has_no_approval(self) -> None:
        self.write_task(self.items[1:])
        spec = self.spec("operational")
        before = self.task.read_bytes()
        receipt = reconcile(self.root, spec, apply=False).split("receipt sha256=")[1].split(";")[0]
        with self.assertRaisesRegex(ValueError, "reviewed scorer receipt authority is absent"):
            reconcile(self.root, spec, apply=True, expected_receipt_sha256=receipt)
        self.assertEqual(before, self.task.read_bytes())


class FakeMailbox:
    def __init__(self, messages: dict[str, EmailMessage], threads: dict[str, bytes]):
        self.messages = messages
        self.threads = threads
        self.selected = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def login(self, *_args):
        return "OK", []

    def select(self, name: str, readonly: bool = False):
        self.selected = name
        return "OK", [b"1"]

    def uid(self, command: str, *args):
        if command == "search":
            message_id = args[-1].strip('"')
            available = message_id in self.messages and (self.selected == '"INBOX"' and message_id.startswith("<human-") or self.selected == '"[Gmail]/Sent Mail"' and message_id.startswith("<sent-"))
            self.last_search_message_id = message_id
            return "OK", [b"1" if available else b""]
        message_id = self.last_search_message_id
        return "OK", [(b"1 (X-GM-THRID " + self.threads[message_id] + b")", self.messages[message_id].as_bytes(policy=policy.default))]


class MailEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages: dict[str, EmailMessage] = {}
        self.sources: dict[str, bytes] = {}
        self.threads: dict[str, bytes] = {}
        for identifier, date in (
            ("2172", "Sat, 26 Sep 2026 10:00:00 -0700"),
            ("2175", "Sat, 26 Sep 2026 10:05:00 -0700"),
            ("2176", "Sat, 26 Sep 2026 10:10:00 -0700"),
            ("2229", "Sun, 27 Sep 2026 10:00:00 -0700"),
            ("2233", "Sun, 27 Sep 2026 11:00:00 -0700"),
            ("2245", "Mon, 28 Sep 2026 10:00:00 -0700"),
        ):
            message_id = f"<human-{identifier}@example.test>"
            message = EmailMessage()
            message["Message-ID"] = message_id
            message["From"] = "human@example.test"
            message["To"] = "agent@example.test"
            message["Subject"] = f"test {identifier}"
            message["Date"] = date
            message.set_content(f"request {identifier}")
            self.messages[message_id] = message
            self.sources[identifier] = f"Subject: test {identifier}\n\nrequest {identifier}".encode()
            self.threads[message_id] = b"2" if identifier == "2172" else b"1"
        self.sent_id = "<sent-archive@example.test>"
        sent = EmailMessage()
        sent["Message-ID"] = self.sent_id
        sent["From"] = "agent@example.test"
        sent["To"] = "human@example.test"
        sent["Subject"] = "Re: scorer"
        sent["References"] = "<human-2245@example.test>"
        sent["Date"] = "Mon, 28 Sep 2026 11:00:00 -0700"
        sent.set_content("stale historical task; leave its disposition to the manager")
        self.messages[self.sent_id] = sent
        self.threads[self.sent_id] = b"1"
        self.spec = {"mode": "archive", "sources": {identifier: {"message_id": f"<human-{identifier}@example.test>"} for identifier in ("2172", "2245")}}
        self.patches = [
            patch(
                "omo_manager.omo_scorer_pending_reconcile.configured_agent_mail",
                return_value=SimpleNamespace(agent_address="agent@example.test", human_address="human@example.test", app_password="test"),
            ),
            patch("omo_manager.omo_scorer_pending_reconcile.exact_human_sender", return_value=True),
            patch.dict(
                "omo_manager.omo_scorer_pending_reconcile.SENT",
                {"archive": (self.sent_id, digest(str(sent["Subject"]).encode()), digest(ordinary_sent_text(sent).encode()), ("stale historical task", "leave its disposition to the manager"))},
            ),
            patch("omo_manager.omo_scorer_pending_reconcile.imaplib.IMAP4_SSL", side_effect=lambda *_args, **_kwargs: FakeMailbox(self.messages, self.threads)),
        ]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def test_exact_original_and_reply_pass(self) -> None:
        verify_mail(self.spec, self.sources)

    def test_changed_thread_and_recipient_fail(self) -> None:
        self.threads[self.sent_id] = b"3"
        with self.assertRaisesRegex(ValueError, "reply does not prove"):
            verify_mail(self.spec, self.sources)
        self.threads[self.sent_id] = b"1"
        self.messages[self.sent_id].replace_header("To", "wrong@example.test")
        with self.assertRaisesRegex(ValueError, "reply does not prove"):
            verify_mail(self.spec, self.sources)

    def test_changed_source_and_missing_reference_fail(self) -> None:
        self.sources["2172"] = b"Subject: test 2172\n\nforged request"
        with self.assertRaisesRegex(ValueError, "source 2172 differs"):
            verify_mail(self.spec, self.sources)
        self.sources["2172"] = b"Subject: test 2172\n\nrequest 2172"
        self.messages[self.sent_id].replace_header("References", "<other@example.test>")
        with self.assertRaisesRegex(ValueError, "reply does not prove"):
            verify_mail(self.spec, self.sources)

    def test_operational_cross_thread_and_late_source_do_not_grant_approval(self) -> None:
        operational = {"mode": "operational", "sources": {identifier: {"message_id": f"<human-{identifier}@example.test>"} for identifier in ("2175", "2176", "2229", "2233", "2245")}}
        self.spec = operational
        sent = self.messages[self.sent_id]
        sent.set_content("virtual environment sys.executable IRM=-1-to-NULL migration is complete 10,000-site raw Common Crawl frame CC study owner handles")
        with patch.dict(
            "omo_manager.omo_scorer_pending_reconcile.SENT",
            {
                "operational": (
                    self.sent_id,
                    digest(str(sent["Subject"]).encode()),
                    digest(ordinary_sent_text(sent).encode()),
                    ("virtual environment", "sys.executable", "IRM=-1-to-NULL migration is complete", "10,000-site raw Common Crawl frame", "CC study owner handles"),
                )
            },
        ):
            verify_mail(operational, self.sources)
            self.messages["<human-2175@example.test>"].replace_header("Date", "Tue, 29 Sep 2026 10:00:00 -0700")
            with self.assertRaisesRegex(ValueError, "reply does not prove"):
                verify_mail(operational, self.sources)


if __name__ == "__main__":
    unittest.main()
