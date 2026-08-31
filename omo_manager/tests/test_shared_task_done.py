from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_shared_task_done import ALL_MAIL_UID, ALL_MAIL_UIDVALIDITY, BODY_SHA256, GMAIL_MESSAGE_ID, GMAIL_THREAD_ID, INITIAL_BLOCKER, INTERNALDATE_UNIX_MS, MESSAGE_ID, OTHER_OWNER_NAME, OUTCOME, RAW_SHA256, SCHEMA, SENT_MAIL_UID, SENT_MAIL_UIDVALIDITY, SUBJECT, TASK_NAME, TASK_TARGET, THREAD_ROOT_MESSAGE_ID, reconcile
from omo_manager.omo_task_metadata import parse_task_metadata


ITEMS = (
    "Find me a good transcription software by searching online using the multiple tools we have.",
    "The transcription software should give timeline, distinguish multiple speakers and be very accurate.",
    "It can be a command line tool, must be open source, and it could be a user graphics interface tool supporting Mac OS and Linux.",
)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def task_text(
    *, status: str = "blocked", target: str = TASK_TARGET, manager: str = "wl:1", is_manager: bool = False, items: tuple[str, ...] = ITEMS, blocker: str = INITIAL_BLOCKER
) -> str:
    queue = "pending_task_items: []\n" if not items else "pending_task_items:\n" + "".join(f"  - {item}\n" for item in items)
    blocker_row = f"blocked_on: {blocker}\n" if status == "blocked" else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocker_row}"
        f"runat: {target}\n"
        "tool: codex\n"
        f"managerat: {manager}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        f"{queue}"
        "---\n"
    )


def owner_text(*, status: str = "blocked", target: str = TASK_TARGET, is_manager: bool = True) -> str:
    return task_text(status=status, target=target, manager="wl:30", is_manager=is_manager, items=("preserve research",), blocker="paused by Human")


class SharedTaskDoneTest(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        task = root / TASK_NAME
        other = root / OTHER_OWNER_NAME
        todo = root / "TODO.md"
        private = root / "private"
        private.mkdir(mode=0o700)
        receipt = private / "adoption.json"
        task_payload = task_text().encode()
        task.write_bytes(task_payload)
        other.write_text(owner_text(), encoding="utf-8")
        todo.write_text(
            "current:\n"
            f"{TASK_NAME} {TASK_TARGET}\n"
            f"{OTHER_OWNER_NAME} {TASK_TARGET}\n"
            "human pending:\n"
            "previous:\n",
            encoding="utf-8",
        )
        value = {
            "schema": SCHEMA,
            "root": str(root),
            "task": TASK_NAME,
            "task_sha256": sha256(task_payload),
            "owner": TASK_TARGET,
            "outcome": OUTCOME,
            "pending_task_items": list(ITEMS),
            "mail_policy": "already-delivered-no-resend",
            "message_id": MESSAGE_ID,
            "thread_root_message_id": THREAD_ROOT_MESSAGE_ID,
            "subject": SUBJECT,
            "provider": "gmail-agent-sent",
            "all_mail_uid": ALL_MAIL_UID,
            "all_mail_uidvalidity": ALL_MAIL_UIDVALIDITY,
            "sent_mail_uid": SENT_MAIL_UID,
            "sent_mail_uidvalidity": SENT_MAIL_UIDVALIDITY,
            "gmail_message_id": GMAIL_MESSAGE_ID,
            "gmail_thread_id": GMAIL_THREAD_ID,
            "internaldate_unix_ms": INTERNALDATE_UNIX_MS,
            "raw_sha256": RAW_SHA256,
            "body_sha256": BODY_SHA256,
            "thread_message_ids": [THREAD_ROOT_MESSAGE_ID, MESSAGE_ID],
        }
        receipt.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        receipt.chmod(0o600)
        return task, other, todo, receipt

    def run_reconcile(self, root: Path, *, task_digest: str | None = None, todo_digest: str | None = None, owner_digest: str | None = None, receipt_digest: str | None = None) -> None:
        task, other, todo, receipt = root / TASK_NAME, root / OTHER_OWNER_NAME, root / "TODO.md", root / "private/adoption.json"
        reconcile(
            root,
            task_digest or sha256(task.read_bytes()),
            todo_digest or sha256(todo.read_bytes()),
            owner_digest or sha256(other.read_bytes()),
            receipt,
            receipt_digest or sha256(receipt.read_bytes()),
        )

    def test_success_changes_only_task_and_todo_and_preserves_shared_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, other, todo, receipt = self.fixture(root)
            other_before = other.read_bytes()
            receipt_before = receipt.read_bytes()
            self.run_reconcile(root)
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual((), metadata.pending_task_items)
            self.assertEqual(other_before, other.read_bytes())
            self.assertEqual(receipt_before, receipt.read_bytes())
            todo_text = todo.read_text(encoding="utf-8")
            self.assertIn(f"current:\n{OTHER_OWNER_NAME} {TASK_TARGET}", todo_text)
            self.assertIn(f"previous:\n{TASK_NAME} {TASK_TARGET}", todo_text)

    def test_rejects_task_queue_target_type_status_and_manager_ownership_drift(self) -> None:
        cases = {
            "queue": task_text(items=ITEMS[:-1]),
            "target": task_text(target="wl:31"),
            "type": task_text(is_manager=True),
            "status": task_text(status="running"),
            "manager": task_text(manager="wl:2"),
            "blocker": task_text(blocker="other"),
        }
        for case, changed in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, other, todo, receipt = self.fixture(root)
                task.write_text(changed, encoding="utf-8")
                record = json.loads(receipt.read_text(encoding="utf-8"))
                record["task_sha256"] = sha256(changed.encode())
                receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                before = (task.read_bytes(), other.read_bytes(), todo.read_bytes())
                with self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assertEqual(before, (task.read_bytes(), other.read_bytes(), todo.read_bytes()))

    def test_rejects_missing_distinct_owner_and_additional_or_ambiguous_owner(self) -> None:
        for case in ("inactive", "wrong target", "zero-padded target", "pane-zero target", "not manager", "additional", "malformed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, other, todo, _receipt = self.fixture(root)
                if case == "inactive":
                    other.write_text(owner_text(status="done"), encoding="utf-8")
                elif case == "wrong target":
                    other.write_text(owner_text(target="wl:31"), encoding="utf-8")
                elif case == "zero-padded target":
                    other.write_text(owner_text(target="wl:032"), encoding="utf-8")
                elif case == "pane-zero target":
                    other.write_text(owner_text(target="wl:32.0"), encoding="utf-8")
                elif case == "not manager":
                    other.write_text(owner_text(is_manager=False), encoding="utf-8")
                elif case == "additional":
                    (root / "third.md").write_text(owner_text(), encoding="utf-8")
                else:
                    (root / "third.md").write_text("---\nrunat: wl:032\nrunat: wl:31\n---\n", encoding="utf-8")
                before = (task.read_bytes(), other.read_bytes(), todo.read_bytes())
                with self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assertEqual(before, (task.read_bytes(), other.read_bytes(), todo.read_bytes()))

    def test_rejects_task_todo_owner_receipt_and_receipt_content_binding_drift(self) -> None:
        for case in ("task digest", "todo digest", "owner digest", "receipt digest", "receipt root", "receipt task", "receipt queue", "receipt provider", "receipt policy"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, other, todo, receipt = self.fixture(root)
                kwargs: dict[str, str] = {}
                if case.endswith("digest"):
                    key = case.replace(" digest", "_digest")
                    kwargs[key] = "0" * 64
                else:
                    record = json.loads(receipt.read_text(encoding="utf-8"))
                    if case == "receipt root":
                        record["root"] = "/wrong"
                    elif case == "receipt task":
                        record["task"] = "other.md"
                    elif case == "receipt queue":
                        record["pending_task_items"] = list(reversed(ITEMS))
                    elif case == "receipt provider":
                        record["gmail_thread_id"] = "1"
                    else:
                        record["mail_policy"] = "resend-allowed"
                    receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                before = (task.read_bytes(), other.read_bytes(), todo.read_bytes())
                with self.assertRaises(OSError):
                    self.run_reconcile(root, **kwargs)
                self.assertEqual(before, (task.read_bytes(), other.read_bytes(), todo.read_bytes()))

    def test_rejects_concurrent_task_todo_owner_or_receipt_mutation_before_write(self) -> None:
        for case in ("task", "todo", "owner", "receipt"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, other, todo, receipt = self.fixture(root)
                original = __import__("omo_manager.omo_shared_task_done", fromlist=["read_private_file"]).read_private_file
                calls = 0

                def mutate(path: Path, digest: str) -> bytes:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        changed = {"task": task, "todo": todo, "owner": other, "receipt": receipt}[case]
                        changed.write_bytes(changed.read_bytes() + b"drift\n")
                    return original(path, digest)

                before_task = task.read_bytes()
                before_todo = todo.read_bytes()
                with patch("omo_manager.omo_shared_task_done.read_private_file", side_effect=mutate), self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assertEqual(before_task, task.read_bytes()[: len(before_task)])
                self.assertEqual(before_todo, todo.read_bytes()[: len(before_todo)])

    def test_no_tmux_or_mail_api_is_imported(self) -> None:
        source = (Path(__file__).parents[1] / "omo_shared_task_done.py").read_text(encoding="utf-8")
        for forbidden in (
            "omo_agent_status",
            "omo_codex_status",
            "omo_tmux_send",
            "omo_codex_stop",
            "omo_task_status",
            "omo_task_edit",
            "omo_completion_mail_adopt",
            "omo_completion_email",
            "email_me",
            "send_completion_email",
            "kill-pane",
            "send-keys",
        ):
            self.assertNotIn(forbidden, source)

    def test_write_set_never_contains_distinct_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _task, other, _todo, _receipt = self.fixture(root)
            writes: list[Path] = []
            from omo_manager import omo_shared_task_done as helper

            original = helper.replace_if_unchanged

            def observe(path: Path, payload: bytes, before: object) -> None:
                writes.append(path)
                original(path, payload, before)  # pyright: ignore[reportArgumentType]

            with patch("omo_manager.omo_shared_task_done.replace_if_unchanged", side_effect=observe):
                self.run_reconcile(root)
            self.assertNotIn(other, writes)

    def test_rejects_concurrent_membership_before_commit_and_rolls_back_after_commit(self) -> None:
        for injection_call in (2, 3):
            with self.subTest(injection_call=injection_call), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, other, todo, _receipt = self.fixture(root)
                before = (task.read_bytes(), other.read_bytes(), todo.read_bytes())
                from omo_manager import omo_shared_task_done as helper

                original = helper.task_paths
                calls = 0

                def inject(current_root: Path) -> tuple[Path, ...]:
                    nonlocal calls
                    calls += 1
                    if calls == injection_call:
                        (root / "third.md").write_text(owner_text(), encoding="utf-8")
                    return original(current_root)

                with patch("omo_manager.omo_shared_task_done.task_paths", side_effect=inject), self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assertEqual(before, (task.read_bytes(), other.read_bytes(), todo.read_bytes()))


if __name__ == "__main__":
    unittest.main()
