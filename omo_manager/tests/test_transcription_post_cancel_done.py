from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_shared_task_done as shared
from omo_manager import omo_transcription_post_cancel_done as helper
from omo_manager.omo_shared_task_done import (
    ADOPTION_SCHEMA,
    ALL_MAIL_UID,
    ALL_MAIL_UIDVALIDITY,
    BODY_SHA256,
    GMAIL_MESSAGE_ID,
    GMAIL_THREAD_ID,
    INTERNALDATE_UNIX_MS,
    MESSAGE_ID,
    OUTCOME,
    RAW_SHA256,
    SENT_MAIL_UID,
    SENT_MAIL_UIDVALIDITY,
    SUBJECT,
    THREAD_ROOT_MESSAGE_ID,
)
from omo_manager.omo_task_metadata import parse_task_metadata

AUTHORITY = (
    "Subject: Re: Approve transcription task closure\n\n"
    "approve\n\n"
    "> Please approve marking only the task “Find me a good transcription software” complete.\n"
    "> I verified that the update targets only this task. It will mark the task done while keeping it and its history visible in the completed list. It will not change any other task, send email, or rerun research.\n"
).encode()


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def file_binding(path: Path) -> helper.FileBinding:
    state = path.stat()
    return helper.FileBinding(state.st_dev, state.st_ino, state.st_size, state.st_mtime_ns, state.st_mode & 0o777, state.st_uid, state.st_gid)


def task_text(
    *,
    status: str = "blocked",
    target: str = helper.TARGET,
    manager: str = helper.TASK_MANAGER,
    is_manager: bool = False,
    items: tuple[str, ...] = helper.CURRENT_ITEMS,
    blocker: str = helper.INITIAL_BLOCKER,
) -> str:
    queue = "pending_task_items: []\n" if not items else "pending_task_items:\n" + "".join(f"  - {item}\n" for item in items)
    blocked = f"blocked_on: {blocker}\n" if status == "blocked" else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocked}"
        f"runat: {target}\n"
        "tool: codex\n"
        f"managerat: {manager}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        f"{queue}"
        "---\n"
    )


def memory_text(
    *,
    status: str = "done",
    target: str = helper.TARGET,
    manager: str = helper.MEMORY_MANAGER,
    is_manager: bool = True,
    items: tuple[str, ...] = (),
) -> str:
    return task_text(status=status, target=target, manager=manager, is_manager=is_manager, items=items, blocker="paused")


class PostCancelDoneTest(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        task = root / helper.TASK_NAME
        memory = root / helper.MEMORY_NAME
        todo = root / helper.TODO_NAME
        authority = root / helper.AUTHORITY_NAME
        authority.parent.mkdir()
        authority.write_bytes(AUTHORITY)
        authority.chmod(0o600)
        task.write_text(task_text(), encoding="utf-8")
        memory.write_text(memory_text(), encoding="utf-8")
        todo.write_text(
            f"current:\n{helper.TASK_NAME} {helper.TARGET}\nhuman pending:\nprevious:\n{helper.MEMORY_NAME} {helper.TARGET}\n",
            encoding="utf-8",
        )
        private = root / "private"
        private.mkdir(mode=0o700)
        adoption = private / "adoption.json"
        value = {
            "schema": ADOPTION_SCHEMA,
            "root": str(root),
            "task": helper.TASK_NAME,
            "task_sha256": helper.ADOPTED_TASK_SHA256,
            "owner": helper.TARGET,
            "outcome": OUTCOME,
            "pending_task_items": list(helper.DELIVERED_ITEMS),
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
        adoption.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        adoption.chmod(0o600)
        return task, memory, todo, adoption, authority

    def run_reconcile(
        self,
        root: Path,
        *,
        task_digest: str | None = None,
        todo_digest: str | None = None,
        memory_digest: str | None = None,
        adoption_digest: str | None = None,
        authority_digest: str | None = None,
        authority_path: Path | None = None,
        helper_digest: str | None = None,
        membership_digest: str | None = None,
        adoption_binding: helper.FileBinding | None = None,
        authority_binding: helper.FileBinding | None = None,
    ) -> None:
        task = root / helper.TASK_NAME
        memory = root / helper.MEMORY_NAME
        todo = root / helper.TODO_NAME
        adoption = root / "private/adoption.json"
        authority = root / helper.AUTHORITY_NAME
        digest = sha256(AUTHORITY)
        with patch.object(helper, "AUTHORITY_SHA256", digest):
            helper.reconcile(
                helper.Args(
                    root,
                    helper_digest or sha256(Path(helper.__file__).read_bytes()),
                    membership_digest or helper.membership_sha256(shared.task_paths(root)),
                    task_digest or sha256(task.read_bytes()),
                    todo_digest or sha256(todo.read_bytes()),
                    memory_digest or sha256(memory.read_bytes()),
                    adoption,
                    adoption_digest or sha256(adoption.read_bytes()),
                    adoption_binding or file_binding(adoption),
                    authority_path or authority,
                    authority_digest or digest,
                    authority_binding or file_binding(authority),
                )
            )

    def assert_unchanged(self, before: tuple[bytes, ...], paths: tuple[Path, ...]) -> None:
        self.assertEqual(before, tuple(path.read_bytes() for path in paths))

    def test_success_changes_only_transcription_and_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, memory, todo, adoption, authority = self.fixture(root)
            protected = (memory.read_bytes(), adoption.read_bytes(), authority.read_bytes())
            self.run_reconcile(root)
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            assert metadata is not None
            self.assertEqual(("done", (), ""), (metadata.status, metadata.pending_task_items, metadata.blocked_on))
            self.assertEqual(protected, (memory.read_bytes(), adoption.read_bytes(), authority.read_bytes()))
            self.assertEqual("previous", helper.todo_section(todo.read_text(encoding="utf-8"), helper.TASK_NAME))
            self.assertEqual("previous", helper.todo_section(todo.read_text(encoding="utf-8"), helper.MEMORY_NAME))
            self.assertIn(MESSAGE_ID, task.read_text(encoding="utf-8"))

    def test_rejects_authority_path_digest_content_mode_and_scope_drift(self) -> None:
        for case in ("path", "digest", "content", "mode", "approval", "scope"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                authority_path_override: Path | None = None
                authority_digest_override: str | None = None
                if case == "path":
                    other = root / "authority.txt"
                    other.write_bytes(AUTHORITY)
                    other.chmod(0o600)
                    authority_path_override = other
                elif case == "digest":
                    authority_digest_override = "0" * 64
                elif case == "mode":
                    authority.chmod(0o400)
                elif case == "content":
                    authority.write_bytes(AUTHORITY + b"drift\n")
                elif case == "approval":
                    authority.write_bytes(AUTHORITY.replace(b"approve", b"decline", 1))
                else:
                    authority.write_bytes(AUTHORITY.replace(b"only this task", b"several tasks", 1))
                paths = (task, memory, todo, adoption, authority)
                before = tuple(path.read_bytes() for path in paths)
                with self.assertRaises(OSError):
                    self.run_reconcile(root, authority_path=authority_path_override, authority_digest=authority_digest_override)
                self.assert_unchanged(before, paths)

    def test_rejects_helper_membership_and_evidence_identity_binding_drift(self) -> None:
        for case in ("helper", "membership", "adoption identity", "authority identity"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                helper_digest: str | None = None
                membership_digest: str | None = None
                adoption_binding: helper.FileBinding | None = None
                authority_binding: helper.FileBinding | None = None
                if case == "helper":
                    helper_digest = "0" * 64
                elif case == "membership":
                    membership_digest = "0" * 64
                elif case == "adoption identity":
                    binding = file_binding(adoption)
                    adoption_binding = helper.FileBinding(binding.device, binding.inode + 1, binding.size, binding.mtime_ns, binding.mode, binding.uid, binding.gid)
                else:
                    binding = file_binding(authority)
                    authority_binding = helper.FileBinding(binding.device, binding.inode + 1, binding.size, binding.mtime_ns, binding.mode, binding.uid, binding.gid)
                paths = (task, memory, todo, adoption, authority)
                before = tuple(path.read_bytes() for path in paths)
                with self.assertRaises(OSError):
                    self.run_reconcile(
                        root,
                        helper_digest=helper_digest,
                        membership_digest=membership_digest,
                        adoption_binding=adoption_binding,
                        authority_binding=authority_binding,
                    )
                self.assert_unchanged(before, paths)

    def test_rejects_task_queue_target_type_status_manager_and_blocker_drift(self) -> None:
        cases = {
            "queue": task_text(items=helper.CURRENT_ITEMS[:-1]),
            "target": task_text(target="wl:31"),
            "type": task_text(is_manager=True),
            "status": task_text(status="running"),
            "manager": task_text(manager="wl:2"),
            "blocker": task_text(blocker="other"),
        }
        for case, changed in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                task.write_text(changed, encoding="utf-8")
                paths = (task, memory, todo, adoption, authority)
                before = tuple(path.read_bytes() for path in paths)
                with self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assert_unchanged(before, paths)

    def test_rejects_done_memory_history_status_queue_target_type_and_manager_drift(self) -> None:
        cases = {
            "status": memory_text(status="blocked"),
            "queue": memory_text(items=("unfinished",)),
            "target": memory_text(target="wl:31"),
            "type": memory_text(is_manager=False),
            "manager": memory_text(manager="wl:29"),
        }
        for case, changed in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                memory.write_text(changed, encoding="utf-8")
                paths = (task, memory, todo, adoption, authority)
                before = tuple(path.read_bytes() for path in paths)
                with self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assert_unchanged(before, paths)

    def test_rejects_all_byte_bindings_delivery_fields_and_todo_custody_drift(self) -> None:
        for case in ("task digest", "todo digest", "memory digest", "adoption digest", "message", "provider", "thread", "items", "task previous", "memory current", "duplicate"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                task_digest: str | None = None
                todo_digest: str | None = None
                memory_digest: str | None = None
                adoption_digest: str | None = None
                if case.endswith("digest"):
                    if case == "task digest":
                        task_digest = "0" * 64
                    elif case == "todo digest":
                        todo_digest = "0" * 64
                    elif case == "memory digest":
                        memory_digest = "0" * 64
                    else:
                        adoption_digest = "0" * 64
                elif case in {"message", "provider", "thread", "items"}:
                    value = json.loads(adoption.read_text(encoding="utf-8"))
                    if case == "message":
                        value["message_id"] = "<other@gmail.com>"
                    elif case == "provider":
                        value["provider"] = "local"
                    elif case == "thread":
                        value["gmail_thread_id"] = "other"
                    else:
                        value["pending_task_items"] = list(reversed(helper.DELIVERED_ITEMS))
                    adoption.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                elif case == "task previous":
                    todo.write_text(f"current:\nhuman pending:\nprevious:\n{helper.TASK_NAME} {helper.TARGET}\n{helper.MEMORY_NAME} {helper.TARGET}\n", encoding="utf-8")
                elif case == "memory current":
                    todo.write_text(f"current:\n{helper.TASK_NAME} {helper.TARGET}\n{helper.MEMORY_NAME} {helper.TARGET}\nprevious:\n", encoding="utf-8")
                else:
                    todo.write_text(todo.read_text(encoding="utf-8") + f"{helper.TASK_NAME} {helper.TARGET}\n", encoding="utf-8")
                paths = (task, memory, todo, adoption, authority)
                before = tuple(path.read_bytes() for path in paths)
                with self.assertRaises(OSError):
                    self.run_reconcile(
                        root,
                        task_digest=task_digest,
                        todo_digest=todo_digest,
                        memory_digest=memory_digest,
                        adoption_digest=adoption_digest,
                    )
                self.assert_unchanged(before, paths)

    def test_rejects_missing_or_additional_active_owner_and_membership_drift(self) -> None:
        for case in ("inactive transcription", "active memory", "additional", "malformed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                if case == "inactive transcription":
                    task.write_text(task_text(status="done", items=()), encoding="utf-8")
                elif case == "active memory":
                    memory.write_text(memory_text(status="blocked"), encoding="utf-8")
                elif case == "additional":
                    (root / "third.md").write_text(task_text(manager="wl:9"), encoding="utf-8")
                else:
                    (root / "third.md").write_text("---\nrunat: wl:032\nrunat: wl:31\n---\n", encoding="utf-8")
                paths = (task, memory, todo, adoption, authority)
                before = tuple(path.read_bytes() for path in paths)
                with self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assert_unchanged(before, paths)

    def test_rejects_precommit_concurrent_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, memory, todo, _adoption, _authority = self.fixture(root)
            original = shared.active_target_owners
            calls = 0

            def mutate_on_second_call(scan_root: Path, target: str, paths: tuple[Path, ...]) -> tuple[Path, ...]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    memory.write_bytes(memory.read_bytes() + b"drift\n")
                return original(scan_root, target, paths)

            with patch.object(shared, "active_target_owners", side_effect=mutate_on_second_call), self.assertRaises(OSError):
                self.run_reconcile(root)
            self.assertEqual(task_text().encode(), task.read_bytes())
            self.assertIn(b"drift", memory.read_bytes())
            self.assertIn(f"current:\n{helper.TASK_NAME}".encode(), todo.read_bytes())

    def test_rejects_authority_or_adoption_mutation_before_commit(self) -> None:
        for case in ("authority", "adoption"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, memory, todo, adoption, authority = self.fixture(root)
                task_before = task.read_bytes()
                todo_before = todo.read_bytes()
                protected_before = memory.read_bytes()
                target = authority if case == "authority" else adoption
                original = helper.task_replacement

                def mutate_during_plan(
                    plan_root: Path,
                    text: str,
                    adoption_sha256: str,
                    target_path: Path = target,
                    original_func: Callable[[Path, str, str], str] = original,
                ) -> str:
                    target_path.write_bytes(target_path.read_bytes() + b"drift\n")
                    return original_func(plan_root, text, adoption_sha256)

                with patch.object(helper, "task_replacement", side_effect=mutate_during_plan), self.assertRaises(OSError):
                    self.run_reconcile(root)
                self.assertEqual(task_before, task.read_bytes())
                self.assertEqual(todo_before, todo.read_bytes())
                self.assertEqual(protected_before, memory.read_bytes())
                self.assertTrue(target.read_bytes().endswith(b"drift\n"))

    def test_rejects_unrelated_task_concurrent_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, _memory, todo, _adoption, _authority = self.fixture(root)
            unrelated = root / "unrelated.md"
            unrelated.write_text(task_text(status="done", target="wl:9", manager="wl:8", items=()), encoding="utf-8")
            task_before = task.read_bytes()
            todo_before = todo.read_bytes()
            original = shared.active_target_owners
            calls = 0

            def mutate_on_second_call(scan_root: Path, target: str, paths: tuple[Path, ...]) -> tuple[Path, ...]:
                nonlocal calls
                calls += 1
                if calls == 2:
                    unrelated.write_bytes(unrelated.read_bytes() + b"drift\n")
                return original(scan_root, target, paths)

            with patch.object(shared, "active_target_owners", side_effect=mutate_on_second_call), self.assertRaises(OSError):
                self.run_reconcile(root)
            self.assertEqual(task_before, task.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertTrue(unrelated.read_bytes().endswith(b"drift\n"))

    def test_postcommit_protected_drift_rolls_back_only_owned_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, memory, todo, adoption, _authority = self.fixture(root)
            task_before = task.read_bytes()
            todo_before = todo.read_bytes()
            original = shared.finish_transaction

            def mutate_after_commit(
                task_path: Path,
                updated_task: bytes,
                task_before: os.stat_result,
                todo_path: Path,
                todo_payload: bytes,
                updated_todo: bytes,
                todo_before: os.stat_result,
            ) -> None:
                original(task_path, updated_task, task_before, todo_path, todo_payload, updated_todo, todo_before)
                memory.write_bytes(memory.read_bytes() + b"concurrent\n")

            with patch.object(shared, "finish_transaction", side_effect=mutate_after_commit), self.assertRaises(OSError):
                self.run_reconcile(root)
            self.assertEqual(task_before, task.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertTrue(memory.read_bytes().endswith(b"concurrent\n"))
            self.assertTrue(adoption.exists())

    def test_rollback_never_overwrites_concurrent_task_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            todo = root / "TODO.md"
            task.write_bytes(b"updated task\n")
            todo.write_bytes(b"updated todo\n")
            original = shared.same_file_state
            calls = 0

            def replace_before_state_check(left: os.stat_result, right: os.stat_result) -> bool:
                nonlocal calls
                calls += 1
                if calls == 1:
                    task.write_bytes(b"foreign replacement task\n")
                    right = task.stat()
                return original(left, right)

            with patch.object(shared, "same_file_state", side_effect=replace_before_state_check), self.assertRaisesRegex(OSError, "rollback was incomplete"):
                helper.rollback_own_writes(task, b"old task\n", b"updated task\n", todo, b"old todo\n", b"updated todo\n")
            self.assertEqual(b"foreign replacement task\n", task.read_bytes())
            self.assertEqual(b"old todo\n", todo.read_bytes())

    def test_module_has_no_mail_or_tmux_action_dependency(self) -> None:
        source = Path(helper.__file__).read_text(encoding="utf-8")
        self.assertNotIn("imaplib", source)
        self.assertNotIn("smtplib", source)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("tmux", "\n".join(line for line in source.splitlines() if not line.startswith("    print(")))


if __name__ == "__main__":
    unittest.main()
