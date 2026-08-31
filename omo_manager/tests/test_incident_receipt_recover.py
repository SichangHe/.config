from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_incident_receipt_recover import Args, recover, recovery_receipt
from omo_manager.omo_shared_task_done import (
    ADOPTION_SCHEMA,
    ALL_MAIL_UID,
    ALL_MAIL_UIDVALIDITY,
    BODY_SHA256,
    GMAIL_MESSAGE_ID,
    GMAIL_THREAD_ID,
    INTERNALDATE_UNIX_MS,
    MESSAGE_ID,
    OTHER_OWNER_NAME,
    OUTCOME,
    RAW_SHA256,
    RECOVERY_SCHEMA,
    SENT_MAIL_UID,
    SENT_MAIL_UIDVALIDITY,
    SUBJECT,
    TASK_NAME,
    TASK_TARGET,
    THREAD_ROOT_MESSAGE_ID,
)
from omo_manager.omo_production_approval import PROBLEM_ID, SCHEMA as APPROVAL_SCHEMA
from omo_manager import omo_production_approval as approval_helper

ITEMS = (
    "Find me a good transcription software by searching online using the multiple tools we have.",
    "The transcription software should give timeline, distinguish multiple speakers and be very accurate.",
    "It can be a command line tool, must be open source, and it could be a user graphics interface tool supporting Mac OS and Linux.",
)


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def task_text(*, target: str = TASK_TARGET, items: tuple[str, ...] = ITEMS) -> str:
    queue = "pending_task_items:\n" + "".join(f"  - {item}\n" for item in items)
    return (
        "---\n"
        "version: v1.0.0\n"
        "status: blocked\n"
        "blocked_on: owner-authenticated reconciliation of already delivered completion email; no resend\n"
        f"runat: {target}\n"
        "tool: codex\n"
        "managerat: wl:1\n"
        "is_manager: false\n"
        f"{queue}"
        "---\n"
    )


def owner_text() -> str:
    return (
        "---\n"
        "version: v1.0.0\n"
        "status: blocked\n"
        "blocked_on: paused\n"
        f"runat: {TASK_TARGET}\n"
        "tool: codex\n"
        "managerat: wl:30\n"
        "is_manager: true\n"
        "pending_task_items:\n"
        "  - preserve research\n"
        "---\n"
    )


class IncidentReceiptRecoverTest(unittest.TestCase):
    def setUp(self) -> None:
        approval_helper.TRUSTED_APPROVAL_SHA256 = ""

    def tearDown(self) -> None:
        approval_helper.TRUSTED_APPROVAL_SHA256 = ""

    def fixture(self, root: Path) -> tuple[Args, Path, Path, Path, Path]:
        task = root / TASK_NAME
        owner = root / OTHER_OWNER_NAME
        todo = root / "TODO.md"
        task.write_text(task_text(), encoding="utf-8")
        owner.write_text(owner_text(), encoding="utf-8")
        todo.write_text(
            f"current:\n{TASK_NAME} {TASK_TARGET}\n{OTHER_OWNER_NAME} {TASK_TARGET}\nhuman pending:\nprevious:\n",
            encoding="utf-8",
        )
        incident_dir = root / "incident"
        incident_dir.mkdir(mode=0o700)
        incident = incident_dir / "adoption.json"
        incident_value = {
            "schema": ADOPTION_SCHEMA,
            "root": str(root),
            "task": TASK_NAME,
            "task_sha256": sha256(task.read_bytes()),
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
        incident.write_text(json.dumps(incident_value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        incident.chmod(0o600)
        recovery_dir = root / "recovery"
        recovery_dir.mkdir(mode=0o700)
        output = recovery_dir / "recovery.json"
        incident_state = incident.stat()
        approval_dir = root / "approval"
        approval_dir.mkdir(mode=0o700)
        approval = approval_dir / "approval.json"
        args = Args(
            root,
            sha256(task.read_bytes()),
            sha256(todo.read_bytes()),
            sha256(owner.read_bytes()),
            incident,
            sha256(incident.read_bytes()),
            incident_state.st_dev,
            incident_state.st_ino,
            incident_state.st_size,
            incident_state.st_mtime_ns,
            incident_state.st_mode & 0o777,
            incident_state.st_uid,
            incident_state.st_gid,
            output,
            "b" * 64,
            approval,
            "0" * 64,
        )
        recovery_digest = sha256(recovery_receipt(args, incident_state, ITEMS))
        approval_value = {
            "schema": APPROVAL_SCHEMA,
            "watcher_problem": PROBLEM_ID,
            "approved_packet_sha256": args.approved_packet_sha256,
            "approved_actions": ["create-recovery-evidence", "close-shared-task"],
            "root": str(root),
            "task_sha256": args.task_sha256,
            "todo_sha256": args.todo_sha256,
            "protected_owner_sha256": args.other_sha256,
            "incident_receipt_path": str(incident),
            "incident_receipt_sha256": args.incident_sha256,
            "recovery_receipt_path": str(output),
            "recovery_receipt_sha256": recovery_digest,
            "approval_scope": "one-recovery-and-one-closure",
        }
        approval.write_text(json.dumps(approval_value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        approval.chmod(0o400)
        approval_helper.TRUSTED_APPROVAL_SHA256 = sha256(approval.read_bytes())
        args = Args(
            args.root,
            args.task_sha256,
            args.todo_sha256,
            args.other_sha256,
            args.incident_path,
            args.incident_sha256,
            args.incident_device,
            args.incident_inode,
            args.incident_size,
            args.incident_mtime_ns,
            args.incident_mode,
            args.incident_uid,
            args.incident_gid,
            args.output,
            args.approved_packet_sha256,
            args.approval_path,
            sha256(approval.read_bytes()),
        )
        return args, task, todo, owner, incident

    def test_success_preserves_incident_and_production_and_creates_fresh_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, todo, owner, incident = self.fixture(root)
            before = (task.read_bytes(), todo.read_bytes(), owner.read_bytes(), incident.read_bytes(), incident.stat())
            recover(args)
            value = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(RECOVERY_SCHEMA, value["schema"])
            self.assertEqual("preserve-incident-receipt-no-reuse", value["recovery_policy"])
            self.assertEqual("not-contained-separate-explicit-production-approval-required", value["execution_authority"])
            self.assertEqual(str(incident), value["incident_receipt_path"])
            self.assertEqual(sha256(incident.read_bytes()), value["incident_receipt_sha256"])
            self.assertEqual(before[:4], (task.read_bytes(), todo.read_bytes(), owner.read_bytes(), incident.read_bytes()))
            self.assertEqual((before[4].st_dev, before[4].st_ino, before[4].st_mtime_ns), (incident.stat().st_dev, incident.stat().st_ino, incident.stat().st_mtime_ns))
            self.assertEqual(0o600, args.output.stat().st_mode & 0o777)

    def test_rejects_incident_reuse_same_directory_and_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, todo, owner, incident = self.fixture(root)
            for output in (incident, incident.parent / "recovery.json"):
                with self.subTest(output=output), self.assertRaisesRegex(OSError, "mechanically separate"):
                    recover(
                        Args(
                            args.root,
                            args.task_sha256,
                            args.todo_sha256,
                            args.other_sha256,
                            incident,
                            args.incident_sha256,
                            args.incident_device,
                            args.incident_inode,
                            args.incident_size,
                            args.incident_mtime_ns,
                            args.incident_mode,
                            args.incident_uid,
                            args.incident_gid,
                            output,
                            args.approved_packet_sha256,
                            args.approval_path,
                            args.approval_sha256,
                        )
                    )
            args.output.write_text("existing", encoding="utf-8")
            args.output.chmod(0o600)
            with self.assertRaises(FileExistsError):
                recover(args)
            self.assertEqual("existing", args.output.read_text(encoding="utf-8"))
            self.assertEqual(args.task_sha256, sha256(task.read_bytes()))
            self.assertEqual(args.todo_sha256, sha256(todo.read_bytes()))
            self.assertEqual(args.other_sha256, sha256(owner.read_bytes()))

    def test_rejects_stale_task_todo_owner_incident_or_membership(self) -> None:
        for case in ("task", "todo", "owner", "incident", "additional owner"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args, task, todo, owner, incident = self.fixture(root)
                changed = {"task": task, "todo": todo, "owner": owner, "incident": incident}.get(case)
                if changed is not None:
                    changed.write_bytes(changed.read_bytes() + b"drift\n")
                else:
                    (root / "third.md").write_text(owner_text(), encoding="utf-8")
                before = (task.read_bytes(), todo.read_bytes(), owner.read_bytes(), incident.read_bytes())
                with self.assertRaises(OSError):
                    recover(args)
                self.assertFalse(args.output.exists())
                self.assertEqual(before, (task.read_bytes(), todo.read_bytes(), owner.read_bytes(), incident.read_bytes()))

    def test_rejects_same_bytes_incident_identity_replacement_before_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, todo, owner, incident = self.fixture(root)
            payload = incident.read_bytes()
            replacement = incident.with_name("replacement.json")
            replacement.write_bytes(payload)
            replacement.replace(incident)
            incident.chmod(0o600)
            with self.assertRaisesRegex(OSError, "identity drifted"):
                recover(args)
            self.assertFalse(args.output.exists())
            self.assertEqual(args.task_sha256, sha256(task.read_bytes()))
            self.assertEqual(args.todo_sha256, sha256(todo.read_bytes()))
            self.assertEqual(args.other_sha256, sha256(owner.read_bytes()))

    def test_requires_separate_exact_production_approval(self) -> None:
        for case in ("missing", "wrong digest", "wrong packet"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args, _task, _todo, _owner, _incident = self.fixture(root)
                if case == "missing":
                    args.approval_path.chmod(0o600)
                elif case == "wrong digest":
                    args = replace(args, approval_sha256="0" * 64)
                else:
                    args = replace(args, approved_packet_sha256="0" * 64)
                with self.assertRaises(OSError):
                    recover(args)
                self.assertFalse(args.output.exists())

    def test_same_uid_cannot_self_authorize_with_forged_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, _task, _todo, _owner, _incident = self.fixture(root)
            approval_helper.TRUSTED_APPROVAL_SHA256 = ""
            with self.assertRaisesRegex(OSError, "no authenticated production approval"):
                recover(args)
            self.assertFalse(args.output.exists())

    def test_concurrent_drift_after_output_retains_fresh_recovery_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, task, todo, owner, incident = self.fixture(root)
            before_incident = incident.read_bytes()
            from omo_manager import omo_incident_receipt_recover as helper

            original = helper.write_private_exclusive

            def write_then_drift(path: Path, payload: bytes) -> object:
                result = original(path, payload)
                todo.write_bytes(todo.read_bytes() + b"drift\n")
                return result

            with patch("omo_manager.omo_incident_receipt_recover.write_private_exclusive", side_effect=write_then_drift), self.assertRaises(OSError):
                recover(args)
            self.assertTrue(args.output.exists())
            self.assertEqual(0o600, args.output.stat().st_mode & 0o777)
            self.assertEqual(before_incident, incident.read_bytes())
            self.assertEqual(args.task_sha256, sha256(task.read_bytes()))
            self.assertEqual(args.other_sha256, sha256(owner.read_bytes()))

    def test_cleanup_never_unlinks_concurrently_replaced_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, _task, todo, _owner, incident = self.fixture(root)
            before_incident = incident.read_bytes()
            from omo_manager import omo_incident_receipt_recover as helper

            original = helper.write_private_exclusive

            def replace_output_then_drift(path: Path, payload: bytes) -> object:
                result = original(path, payload)
                path.unlink()
                path.write_bytes(b"foreign replacement")
                path.chmod(0o600)
                todo.write_bytes(todo.read_bytes() + b"drift\n")
                return result

            with patch("omo_manager.omo_incident_receipt_recover.write_private_exclusive", side_effect=replace_output_then_drift), self.assertRaises(OSError):
                recover(args)
            self.assertEqual(b"foreign replacement", args.output.read_bytes())
            self.assertEqual(before_incident, incident.read_bytes())

    def test_rejects_recovery_parent_swap_before_exclusive_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, _task, _todo, _owner, incident = self.fixture(root)
            before_incident = incident.read_bytes()
            from omo_manager import omo_incident_receipt_recover as helper

            original = helper.os.open
            swapped = False

            def swap(path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
                nonlocal swapped
                if not swapped and path == args.output.parent and dir_fd is None:
                    swapped = True
                    held = args.output.parent.with_name("held-recovery")
                    args.output.parent.rename(held)
                    args.output.parent.mkdir(mode=0o700)
                return original(path, flags, mode, dir_fd=dir_fd)  # pyright: ignore[reportArgumentType]

            with patch("omo_manager.omo_incident_receipt_recover.os.open", side_effect=swap), self.assertRaisesRegex(OSError, "directory changed"):
                recover(args)
            self.assertFalse(args.output.exists())
            self.assertEqual(before_incident, incident.read_bytes())

    def test_source_has_no_mail_tmux_or_production_replacement_api(self) -> None:
        source = (Path(__file__).parents[1] / "omo_incident_receipt_recover.py").read_text(encoding="utf-8")
        for forbidden in ("email_me", "imaplib", "omo_tmux_send", "omo_codex_stop", "omo_shared_task_done", "send-keys", "os.replace", "todo.write", "task.write", "other.write"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
