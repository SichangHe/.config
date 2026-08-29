from __future__ import annotations

import grp
import hashlib
import os
import pwd
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import override
from unittest.mock import patch

import omo_manager.omo_hees_final_artifact_replace as replace_module
from omo_manager.omo_hees_final_artifact_replace import Args, MANAGER_TARGET, STALE_TARGET, ReplaceError, replace
from omo_manager.omo_task_metadata import TaskMetadata, parse_task_metadata

ITEM = "Freeze one exact artifact, preserve its custody, and report its immutable identity."
BODY = "<manager_delegation from=\"guest_hees:0\">\nPreserve this body exactly.\n</manager_delegation>\n"
CHECKED_PANE_ID = replace_module.checked_pane_id


def sha(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def task_text(*, pending: tuple[str, ...] = (ITEM,), status: str = "blocked", managerat: str = MANAGER_TARGET, runat: str = STALE_TARGET, is_manager: bool = False) -> str:
    queue = "pending_task_items: []" if not pending else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in pending)
    blocker = "blocked_on: reviewed atomic replacement helper invocation\n" if status == "blocked" else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocker}"
        f"runat: {runat}\n"
        "tool: codex\n"
        f"managerat: {managerat}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        f"{queue}\n"
        "---\n"
        f"{BODY}"
    )


def todo_text() -> str:
    return (
        "rules stay\n\n"
        "current:\n"
        "keep.md guest_hees:2\n"
        "hees_1170_policy.md guest_hees:5\n"
        "other.md guest_hees:1\n\n"
        "human pending:\n"
        "waiting.md guest_hees:6\n\n"
        "low priority:\n"
        "later.md\n\n"
        "previous:\n"
        "old.md guest_hees:4\n"
    )


def metadata(text: str, root: Path) -> TaskMetadata:
    parsed = parse_task_metadata(text, root)
    if parsed is None:
        raise AssertionError("expected task frontmatter")
    return parsed


class HeesFinalArtifactReplaceTests(unittest.TestCase):
    pane = patch.object(replace_module, "checked_pane_id", return_value="")

    @override
    def setUp(self) -> None:
        _ = self.pane.start()

    @override
    def tearDown(self) -> None:
        self.pane.stop()

    def make_root(self, root: Path, *, stale: str | None = None, todo: str | None = None) -> tuple[str, str, Args]:
        stale_text = task_text() if stale is None else stale
        todo_value = todo_text() if todo is None else todo
        (root / "hees_1170_policy.md").write_text(stale_text, encoding="utf-8")
        (root / "TODO.md").write_text(todo_value, encoding="utf-8")
        args = Args(root, "hees_1170_policy.md", "hees_final_artifact.md", STALE_TARGET, MANAGER_TARGET, sha(stale_text), sha(todo_value), sha(ITEM))
        return stale_text, todo_value, args

    def test_replaces_only_bound_owner_and_preserves_body_and_other_todo_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_text, todo_value, args = self.make_root(root)

            result = replace(args)

            stale = (root / "hees_1170_policy.md").read_text(encoding="utf-8")
            successor = (root / "hees_final_artifact.md").read_text(encoding="utf-8")
            stale_metadata = metadata(stale, root)
            successor_metadata = metadata(successor, root)
            self.assertEqual("done", stale_metadata.status)
            self.assertEqual((), stale_metadata.pending_task_items)
            self.assertEqual("blocked", successor_metadata.status)
            self.assertEqual((ITEM,), successor_metadata.pending_task_items)
            self.assertEqual("codex", successor_metadata.tool)
            self.assertTrue(stale.endswith(BODY))
            self.assertTrue(successor.endswith(BODY))
            expected = todo_value.replace("hees_1170_policy.md guest_hees:5", "hees_final_artifact.md guest_hees:5", 1).replace("previous:\n", "previous:\nhees_1170_policy.md guest_hees:5\n", 1)
            self.assertEqual(expected, (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertIn("separate supported launch/delivery remains required", result)
            self.assertEqual(stale_text.split("---\n", 2)[2], stale.split("---\n", 2)[2])

    def test_preserves_human_pending_successor_custody(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = todo_text().replace("hees_1170_policy.md guest_hees:5\nother.md", "other.md").replace("waiting.md guest_hees:6", "hees_1170_policy.md guest_hees:5\nwaiting.md guest_hees:6")
            _stale, _todo, args = self.make_root(root, todo=todo)

            _ = replace(args)

            updated = (root / "TODO.md").read_text(encoding="utf-8")
            human_pending = updated.split("human pending:\n", 1)[1].split("\nlow priority:", 1)[0]
            self.assertIn("hees_final_artifact.md guest_hees:5", human_pending)
            self.assertNotIn("hees_1170_policy.md", human_pending)

    def test_rejects_changed_task_or_todo_digest(self) -> None:
        for field in ("stale_sha256", "todo_sha256"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale_text, todo_value, args = self.make_root(root)
                changed = dataclass_replace(args, **{field: "0" * 64})
                with self.assertRaisesRegex(ReplaceError, "digest changed"):
                    replace(changed)
                self.assertEqual(stale_text, (root / "hees_1170_policy.md").read_text(encoding="utf-8"))
                self.assertEqual(todo_value, (root / "TODO.md").read_text(encoding="utf-8"))
                self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_rejects_missing_duplicate_or_wrong_pending_item(self) -> None:
        cases = ((), (ITEM, ITEM), ("different",))
        for pending in cases:
            with self.subTest(pending=pending), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale = task_text(pending=pending)
                _stale, _todo, args = self.make_root(root, stale=stale)
                with self.assertRaisesRegex(ReplaceError, "exactly one pending item"):
                    replace(args)
                self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_rejects_existing_successor_file_or_todo_row(self) -> None:
        for kind in ("file", "row", "backticked", "relative"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = {
                    "file": "old.md guest_hees:4",
                    "row": "hees_final_artifact.md guest_hees:5",
                    "backticked": "`hees_final_artifact.md` guest_hees:5",
                    "relative": "`./hees_final_artifact.md` guest_hees:5",
                }
                todo = todo_text().replace("old.md guest_hees:4", rows[kind])
                _stale, _todo, args = self.make_root(root, todo=todo)
                if kind == "file":
                    (root / "hees_final_artifact.md").write_text("occupied\n", encoding="utf-8")
                with self.assertRaisesRegex(ReplaceError, "successor"):
                    replace(args)

    def test_rejects_backticked_relative_duplicate_stale_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = todo_text().replace("old.md guest_hees:4", "`./hees_1170_policy.md` guest_hees:5")
            _stale, _todo, args = self.make_root(root, todo=todo)
            with self.assertRaisesRegex(ReplaceError, "exact stale row once"):
                replace(args)

    def test_rejects_malformed_stale_todo_rows(self) -> None:
        malformed = (
            "``./hees_1170_policy.md` guest_hees:5",
            "`hees_1170_policy.md guest_hees:5",
            "hees_1170_policy.md` guest_hees:5",
            "`hees_1170_policy.md`` guest_hees:5",
            "x.md hees_1170_policy.md guest_hees:5",
            "hees_1170_policy.md guest_hees:5 extra",
        )
        for row in malformed:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                todo = todo_text().replace("hees_1170_policy.md guest_hees:5", row)
                _stale, _todo, args = self.make_root(root, todo=todo)
                with self.assertRaisesRegex(ReplaceError, "malformed stale|name only the exact stale target"):
                    replace(args)

    def test_previous_heading_without_final_newline_gets_separated_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = todo_text().replace("previous:\nold.md guest_hees:4\n", "previous:")
            _stale, _todo, args = self.make_root(root, todo=todo)

            _ = replace(args)

            self.assertTrue((root / "TODO.md").read_text(encoding="utf-8").endswith("previous:\nhees_1170_policy.md guest_hees:5\n"))

    def test_rejects_role_ownership_session_and_duplicate_target_claimant(self) -> None:
        cases = (
            task_text(is_manager=True),
            task_text(managerat="guest_hees:9"),
            task_text(runat="guest_hees:7"),
            task_text().replace("tool: codex\n", "tool: codex\nsession_id: 00000000-0000-4000-8000-000000000001\n"),
        )
        for stale in cases:
            with self.subTest(stale=stale.splitlines()[2:8]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _stale, _todo, args = self.make_root(root, stale=stale)
                with self.assertRaisesRegex(ReplaceError, "status, role, or tool|ownership"):
                    replace(args)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            (root / "duplicate.md").write_text(task_text(managerat="guest_hees:9"), encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "sole active owner"):
                replace(args)

    def test_rejects_malformed_record_that_still_claims_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            malformed = task_text(managerat="guest_hees:9").replace("runat: guest_hees:5", "runat: guest_hees:5 # malformed")
            (root / "malformed.md").write_text(malformed, encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "invalid frontmatter"):
                replace(args)

    def test_rejects_multiline_malformed_target_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            malformed = task_text(managerat="guest_hees:9").replace("runat: guest_hees:5", "runat: |\n  guest_hees:5")
            (root / "malformed.md").write_text(malformed, encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "invalid frontmatter"):
                replace(args)

    def test_rejects_numeric_alias_target_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            alias = task_text(managerat="guest_hees:9", runat="guest_hees:05")
            (root / "alias.md").write_text(alias, encoding="utf-8")
            with self.assertRaisesRegex(ReplaceError, "sole active owner"):
                replace(args)

    def test_rejects_unreadable_subtree_during_owner_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            hidden = root / "hidden"
            hidden.mkdir()
            (hidden / "duplicate.md").write_text(task_text(managerat="guest_hees:9"), encoding="utf-8")
            hidden.chmod(0)
            try:
                with self.assertRaisesRegex(ReplaceError, "directory .* unreadable"):
                    replace(args)
            finally:
                hidden.chmod(0o700)

    def test_accepts_exact_trusted_group_setgid_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            trust = replace_module.trusted_writer_group()
            os.chown(root, -1, trust.gid)
            root.chmod(0o2775)

            replace_module.prepare(args)

    def test_accepts_trusted_group_writable_regular_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            os.chown(root, -1, replace_module.trusted_writer_group().gid)
            root.chmod(0o2775)
            stale = root / "hees_1170_policy.md"
            os.chown(stale, -1, replace_module.trusted_writer_group().gid)
            stale.chmod(0o664)

            _ = replace(args)

            self.assertTrue((root / "hees_final_artifact.md").is_file())

    def test_rejects_group_writable_regular_task_with_wrong_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_before, todo_before, args = self.make_root(root)
            stale = root / "hees_1170_policy.md"
            self.assertNotEqual(stale.stat().st_gid, replace_module.trusted_writer_group().gid)
            stale.chmod(0o664)

            with self.assertRaisesRegex(ReplaceError, "authenticated regular file"):
                replace(args)

            self.assertEqual(stale_before.encode(), stale.read_bytes())
            self.assertEqual(todo_before.encode(), (root / "TODO.md").read_bytes())

    def test_rejects_extended_acl_on_otherwise_trusted_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            trust = replace_module.trusted_writer_group()
            os.chown(root, -1, trust.gid)
            root.chmod(0o2775)
            real_list = replace_module.os.listxattr

            def unsafe_acl(target: int | str | bytes | os.PathLike[str] | os.PathLike[bytes]):
                return [*real_list(target), "system.posix_acl_access"]

            with patch.object(replace_module.os, "listxattr", side_effect=unsafe_acl), self.assertRaisesRegex(ReplaceError, "extended POSIX ACL"):
                replace(args)

    def test_rejects_unsafe_writable_root_or_child_during_owner_proof(self) -> None:
        for unsafe in ("root group without setgid", "root world", "child group without setgid", "child world"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _stale, _todo, args = self.make_root(root)
                child = root / "child"
                child.mkdir()
                changed = root if unsafe.startswith("root") else child
                original_mode = stat.S_IMODE(changed.stat().st_mode)
                unsafe_bit = stat.S_IWGRP if "group" in unsafe else stat.S_IWOTH
                changed.chmod(original_mode | unsafe_bit)
                try:
                    with self.assertRaisesRegex(ReplaceError, "unsafe or unreadable"):
                        replace(args)
                finally:
                    changed.chmod(original_mode)

    def test_rejects_group_writable_directory_with_wrong_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            self.assertNotEqual(replace_module.trusted_writer_group().gid, root.stat().st_gid)
            root.chmod(0o2775)
            with self.assertRaisesRegex(ReplaceError, "unsafe or unreadable"):
                replace(args)

    def test_rejects_unauthorized_trusted_group_member(self) -> None:
        group = grp.struct_group((replace_module.TRUSTED_WRITER_GROUP, "x", 1014, ["agent"]))
        agent = pwd.struct_passwd(("agent", "x", os.getuid(), 100, "", "/tmp", "/bin/sh"))
        human = pwd.struct_passwd((replace_module.TRUSTED_HUMAN_USER, "x", 1014, 1014, "", "/tmp", "/bin/sh"))
        intruder = pwd.struct_passwd(("intruder", "x", 1015, 1014, "", "/tmp", "/bin/sh"))

        def account(name: str):
            return {"agent": agent, replace_module.TRUSTED_HUMAN_USER: human}[name]

        with (
            patch.object(replace_module.grp, "getgrnam", return_value=group),
            patch.object(replace_module.pwd, "getpwuid", return_value=agent),
            patch.object(replace_module.pwd, "getpwnam", side_effect=account),
            patch.object(replace_module.pwd, "getpwall", return_value=[agent, human, intruder]),
            patch.object(replace_module.os, "getgroups", return_value=[1014]),
            self.assertRaisesRegex(ReplaceError, "unauthorized principal"),
        ):
            replace_module.trusted_writer_group()

    def test_rejects_trusted_group_change_during_owner_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, _args = self.make_root(root)
            trust = replace_module.trusted_writer_group()
            changed = replace_module.TrustedGroup(trust.gid, (*trust.members, ("intruder", 99999)))

            with patch.object(replace_module, "trusted_writer_group", side_effect=(trust, changed)), self.assertRaisesRegex(ReplaceError, "trusted writer group changed"):
                replace_module.markdown_records(root)

    def test_rejects_child_directory_replacement_during_owner_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            child = root / "child"
            child.mkdir()
            (child / "record.md").write_text(task_text(runat="guest_hees:9"), encoding="utf-8")
            original_read = replace_module.read_snapshot_at
            replaced = False

            def replace_child(parent_fd: int, name: str, path: Path, label: str, trust: replace_module.TrustedGroup | None = None):
                nonlocal replaced
                snapshot = original_read(parent_fd, name, path, label, trust)
                if path.parent == child and not replaced:
                    replaced = True
                    os.replace(child, root / "child-owned")
                    child.symlink_to(Path(outside_tmp), target_is_directory=True)
                return snapshot

            with patch.object(replace_module, "read_snapshot_at", side_effect=replace_child), self.assertRaisesRegex(ReplaceError, "directory changed during traversal|directory .* changed during traversal"):
                replace(args)

    def test_fifo_task_record_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.md"
            os.mkfifo(path)

            with self.assertRaisesRegex(ReplaceError, "owner-owned authenticated regular file"):
                replace_module.read_snapshot(path, "record")

    def test_rejects_externally_writable_or_acl_task_file(self) -> None:
        for unsafe in ("mode", "acl"):
            with self.subTest(unsafe=unsafe), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale_before, todo_before, args = self.make_root(root)
                stale_path = root / "hees_1170_policy.md"
                if unsafe == "mode":
                    stale_path.chmod(0o666)
                    context = patch.object(replace_module.os, "listxattr", wraps=os.listxattr)
                else:
                    real_list = replace_module.os.listxattr

                    def file_acl(target: int | str | bytes | os.PathLike[str] | os.PathLike[bytes]):
                        attributes = list(real_list(target))
                        return [*attributes, "system.posix_acl_access"] if isinstance(target, int) and stat.S_ISREG(os.fstat(target).st_mode) else attributes

                    context = patch.object(replace_module.os, "listxattr", side_effect=file_acl)
                with context, self.assertRaisesRegex(ReplaceError, "authenticated regular file|extended POSIX ACL"):
                    replace(args)
                self.assertEqual(stale_before.encode(), stale_path.read_bytes())
                self.assertEqual(todo_before.encode(), (root / "TODO.md").read_bytes())
                self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_rejects_live_or_rebound_stale_target(self) -> None:
        for values in (("%1",), ("", "%1")):
            with self.subTest(values=values), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale_text, todo_value, args = self.make_root(root)
                with patch.object(replace_module, "checked_pane_id", side_effect=values), self.assertRaisesRegex(ReplaceError, "live"):
                    replace(args)
                self.assertEqual(stale_text, (root / "hees_1170_policy.md").read_text(encoding="utf-8"))
                self.assertEqual(todo_value, (root / "TODO.md").read_text(encoding="utf-8"))
                self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_tmux_inventory_failure_is_not_absence(self) -> None:
        failed = subprocess.CompletedProcess(["tmux"], 1, "", "no server running")
        with patch.object(replace_module.subprocess, "run", return_value=failed), self.assertRaisesRegex(ReplaceError, "cannot prove stale-target absence"):
            CHECKED_PANE_ID(STALE_TARGET)

    def test_tmux_matching_row_with_invalid_pane_id_is_unknown(self) -> None:
        invalid = subprocess.CompletedProcess(["tmux"], 0, "guest_hees:5.0\t\n", "")
        with patch.object(replace_module.subprocess, "run", return_value=invalid), self.assertRaisesRegex(ReplaceError, "inventory row is malformed"):
            CHECKED_PANE_ID(STALE_TARGET)

    def test_tmux_numeric_alias_matching_row_is_live(self) -> None:
        live_alias = subprocess.CompletedProcess(["tmux"], 0, "guest_hees:05.00\t%1\n", "")
        with patch.object(replace_module.subprocess, "run", return_value=live_alias):
            self.assertEqual("%1", CHECKED_PANE_ID(STALE_TARGET))

    def test_crlf_task_body_and_unaffected_todo_bytes_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = task_text().replace("\n", "\r\n") + "tail-without-newline"
            todo = todo_text().replace("\n", "\r\n")
            _stale, _todo, args = self.make_root(root, stale=stale, todo=todo)

            _ = replace(args)

            stale_after = (root / "hees_1170_policy.md").read_bytes()
            successor = (root / "hees_final_artifact.md").read_bytes()
            body = BODY.replace("\n", "\r\n").encode() + b"tail-without-newline"
            self.assertTrue(stale_after.endswith(body))
            self.assertTrue(successor.endswith(body))
            self.assertNotIn(b"\n", (root / "TODO.md").read_bytes().replace(b"\r\n", b""))

    def test_exclusive_create_race_never_removes_other_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hees_final_artifact.md"
            path.write_bytes(b"other writer\n")
            with self.assertRaises(FileExistsError):
                replace_module.create_successor(path, b"ours\n", 0o644)
            self.assertEqual(b"other writer\n", path.read_bytes())

    def test_create_cleanup_refuses_rebound_path(self) -> None:
        for name in ("hees_final_artifact.md", replace_module.JOURNAL_NAME):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                real_link = replace_module.link_fd

                def rebind_after_link(fd: int, parent_fd: int, published_name: str) -> None:
                    real_link(fd, parent_fd, published_name)
                    os.unlink(path)
                    path.write_bytes(b"other writer\n")

                with patch.object(replace_module, "link_fd", side_effect=rebind_after_link), self.assertRaisesRegex(ReplaceError, "rebound.*manual recovery"):
                    replace_module.create_successor(path, b"ours\n", 0o644)
                self.assertEqual(b"other writer\n", path.read_bytes())

    def test_publication_snapshot_is_stable_after_anonymous_fd_closes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hees_final_artifact.md"
            snapshot = replace_module.create_successor(path, b"ours\n", 0o644)
            replace_module.require_snapshot(snapshot, "successor task")
            self.assertEqual(snapshot.state.st_ctime_ns, path.stat().st_ctime_ns)

    def test_interrupted_partial_transaction_is_recovered_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_create = replace_module.create_successor
            calls = 0

            def interrupt_successor(path: Path, data: bytes, mode: int, root_fd: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise SystemExit("simulated crash")
                return original_create(path, data, mode, root_fd)

            with patch.object(replace_module, "create_successor", side_effect=interrupt_successor), self.assertRaises(SystemExit):
                replace(args)
            self.assertTrue((root / replace_module.JOURNAL_NAME).is_file())
            self.assertEqual("done", metadata((root / "hees_1170_policy.md").read_text(encoding="utf-8"), root).status)

            result = replace(args)

            self.assertTrue((root / replace_module.JOURNAL_NAME).exists())
            self.assertEqual("done", metadata((root / "hees_1170_policy.md").read_text(encoding="utf-8"), root).status)
            self.assertEqual((ITEM,), metadata((root / "hees_final_artifact.md").read_text(encoding="utf-8"), root).pending_task_items)
            self.assertIn("separate supported launch/delivery", result)

    def test_interrupted_complete_transaction_is_finalized_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_owners = replace_module.active_owners
            calls = 0

            def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise SystemExit("simulated crash")
                return original_owners(scan_root, target, overrides, root_fd)

            with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                replace(args)
            self.assertTrue((root / replace_module.JOURNAL_NAME).is_file())

            result = replace(args)

            self.assertTrue((root / replace_module.JOURNAL_NAME).exists())
            self.assertIn("recovered committed replacement", result)

    def test_committed_recovery_rejects_public_root_substitution_at_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_owners = replace_module.active_owners
            calls = 0

            def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise SystemExit("simulated crash")
                return original_owners(scan_root, target, overrides, root_fd)

            with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                replace(args)
            original_root_check = replace_module.require_root_identity
            recovery_checks = 0

            def substitute_at_final(identity: replace_module.RootIdentity) -> None:
                nonlocal recovery_checks
                recovery_checks += 1
                if recovery_checks == 2:
                    raise ReplaceError("work-log root identity changed during replacement")
                original_root_check(identity)

            with patch.object(replace_module, "require_root_identity", side_effect=substitute_at_final), self.assertRaisesRegex(ReplaceError, "root identity changed"):
                replace(args)

    def test_recovery_rejects_committed_self_consistent_noncanonical_after_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            plan = replace_module.prepare(args)
            receipt = replace_module.journal_data(plan, args)
            loaded = replace_module.json.loads(receipt)
            changed = plan.successor_data.replace(ITEM.encode(), b"different item")
            loaded["successor_data"] = replace_module.encoded(changed)
            commitment = loaded.pop("commitment_sha256")
            self.assertIsInstance(commitment, str)
            loaded["commitment_sha256"] = replace_module.digest(replace_module.json.dumps(loaded, sort_keys=True, separators=(",", ":")).encode())
            receipt_path = root / replace_module.JOURNAL_NAME
            receipt_path.write_text(replace_module.json.dumps(loaded, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            receipt_path.chmod(0o600)

            with self.assertRaisesRegex(ReplaceError, "after-state is not the canonical reconstruction"):
                replace(args)

            self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_oversized_prospective_journal_changes_no_lifecycle_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = task_text() + "x" * (replace_module.MAX_JOURNAL_BYTES // 3)
            stale_before, todo_before, args = self.make_root(root, stale=stale)

            with self.assertRaisesRegex(ReplaceError, "prospective transaction journal exceeds"):
                replace(args)

            self.assertEqual(stale_before.encode(), (root / "hees_1170_policy.md").read_bytes())
            self.assertEqual(todo_before.encode(), (root / "TODO.md").read_bytes())
            self.assertFalse((root / "hees_final_artifact.md").exists())
            self.assertFalse((root / replace_module.JOURNAL_NAME).exists())

    def test_full_receipt_budget_changes_no_lifecycle_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_before, todo_before, args = self.make_root(root)
            plan = replace_module.prepare(args)
            receipt_path = root / replace_module.JOURNAL_NAME
            receipt_path.write_bytes(replace_module.journal_data(plan, args))
            receipt_path.chmod(0o600)
            for index in range(replace_module.MAX_DISPLACED_RECEIPTS):
                path = root / f".hees_1170_policy.md.omo-stage-{index:032x}"
                path.write_bytes(plan.stale.data)
                path.chmod(stat.S_IMODE(plan.stale.state.st_mode))

            with self.assertRaisesRegex(ReplaceError, "receipt capacity is insufficient"):
                replace(args)

            self.assertEqual(stale_before.encode(), (root / "hees_1170_policy.md").read_bytes())
            self.assertEqual(todo_before.encode(), (root / "TODO.md").read_bytes())
            self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_committed_recovery_sync_failure_retains_retryable_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_owners = replace_module.active_owners
            calls = 0

            def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise SystemExit("simulated crash")
                return original_owners(scan_root, target, overrides, root_fd)

            with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                replace(args)
            receipt = root / replace_module.JOURNAL_NAME
            receipt_before = receipt.read_bytes()

            with patch.object(replace_module, "fsync_directory", side_effect=OSError("injected sync failure")), self.assertRaisesRegex(ReplaceError, "directory sync failed; retry required"):
                replace(args)

            self.assertEqual(receipt_before, receipt.read_bytes())
            self.assertIn("recovered committed replacement", replace(args))

    def test_committed_recovery_rejects_changed_successor_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_owners = replace_module.active_owners
            calls = 0

            def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise SystemExit("simulated crash")
                return original_owners(scan_root, target, overrides, root_fd)

            with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                replace(args)
            (root / "hees_final_artifact.md").chmod(0o666)

            with self.assertRaisesRegex(ReplaceError, "changed successor mode|authenticated regular file"):
                replace(args)

    def test_todo_concurrent_change_rolls_back_stale_and_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_text, todo_value, args = self.make_root(root)
            original_create = replace_module.create_successor

            def mutate_todo(path: Path, data: bytes, mode: int, root_fd: int | None = None):
                created = original_create(path, data, mode, root_fd)
                (root / "TODO.md").write_text(todo_value + "concurrent\n", encoding="utf-8")
                return created

            with patch.object(replace_module, "create_successor", side_effect=mutate_todo), self.assertRaisesRegex(ReplaceError, "all changes rolled back"):
                replace(args)
            self.assertEqual(stale_text, (root / "hees_1170_policy.md").read_text(encoding="utf-8"))
            self.assertEqual(todo_value + "concurrent\n", (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_todo_write_failure_rolls_back_every_completed_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_text, todo_value, args = self.make_root(root)
            original_replace = replace_module.replace_existing

            def fail_todo(expected: replace_module.Snapshot, data: bytes, root_fd: int | None = None):
                if expected.path.name == "TODO.md" and data != todo_value.encode():
                    raise OSError("injected TODO write failure")
                return original_replace(expected, data, root_fd)

            with patch.object(replace_module, "replace_existing", side_effect=fail_todo), self.assertRaisesRegex(ReplaceError, "all changes rolled back"):
                replace(args)
            self.assertEqual(stale_text, (root / "hees_1170_policy.md").read_text(encoding="utf-8"))
            self.assertEqual(todo_value, (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_post_replace_sync_failure_tracks_committed_write_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_text, todo_value, args = self.make_root(root)
            original_replace = replace_module.replace_existing
            injected = False

            def fail_after_stale_commit(expected: replace_module.Snapshot, data: bytes, root_fd: int | None = None):
                nonlocal injected
                snapshot = original_replace(expected, data, root_fd)
                if expected.path.name == "hees_1170_policy.md" and data != stale_text.encode() and not injected:
                    injected = True
                    raise replace_module.CommittedWriteError(snapshot, OSError("injected directory sync failure"))
                return snapshot

            with patch.object(replace_module, "replace_existing", side_effect=fail_after_stale_commit), self.assertRaisesRegex(ReplaceError, "all changes rolled back"):
                replace(args)
            self.assertEqual(stale_text, (root / "hees_1170_policy.md").read_text(encoding="utf-8"))
            self.assertEqual(todo_value, (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertTrue((root / replace_module.JOURNAL_NAME).is_file())
            self.assertFalse((root / "hees_final_artifact.md").exists())

    def test_stage_replacement_immediately_before_exchange_restores_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.md"
            path.write_bytes(b"before\n")
            expected = replace_module.read_snapshot(path, "record")
            replacement = b"after\n"
            real_exchange = replace_module.rename_exchange
            observed_stages: list[Path] = []
            injected = False

            def replace_stage(parent_fd: int, left: str, right: str) -> None:
                nonlocal injected
                if not injected:
                    injected = True
                    observed_stage = path.parent / left
                    observed_stages.append(observed_stage)
                    os.replace(path.parent / left, path.parent / f"{left}.owned")
                    (path.parent / left).write_bytes(b"foreign\n")
                real_exchange(parent_fd, left, right)

            with patch.object(replace_module, "rename_exchange", side_effect=replace_stage), self.assertRaisesRegex(ReplaceError, "stage changed"):
                _ = replace_module.replace_existing(expected, replacement)
            self.assertEqual(b"before\n", path.read_bytes())
            self.assertEqual(1, len(observed_stages))
            self.assertEqual(b"foreign\n", observed_stages[0].read_bytes())

    def test_identical_content_target_rebind_after_exchange_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.md"
            path.write_bytes(b"before\n")
            expected = replace_module.read_snapshot(path, "record")
            replacement = b"after\n"
            original_read = replace_module.read_snapshot_at
            rebound = False

            def rebind_target(parent_fd: int, name: str, target: Path, label: str, trust: replace_module.TrustedGroup | None = None):
                nonlocal rebound
                if name == path.name and not rebound and path.read_bytes() == replacement:
                    rebound = True
                    os.replace(path, path.with_name("committed-owned"))
                    path.write_bytes(replacement)
                return original_read(parent_fd, name, target, label, trust)

            with patch.object(replace_module, "read_snapshot_at", side_effect=rebind_target), self.assertRaisesRegex(ReplaceError, "rebound after committed replacement"):
                replace_module.replace_existing(expected, replacement)

    def test_final_proof_rejects_identical_or_changed_successor_rebind(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _stale, _todo, args = self.make_root(root)
                original_validate = replace_module.validate_displaced_receipts
                rebound = False

                def rebind_before_final(root_path: Path, journal: replace_module.Journal | None, root_fd: int | None = None):
                    nonlocal rebound
                    receipts = original_validate(root_path, journal, root_fd)
                    successor = root / "hees_final_artifact.md"
                    if successor.exists() and not rebound:
                        rebound = True
                        data = b"changed\n" if changed else successor.read_bytes()
                        os.replace(successor, root / "successor-owned")
                        successor.write_bytes(data)
                    return receipts

                with patch.object(replace_module, "validate_displaced_receipts", side_effect=rebind_before_final), self.assertRaisesRegex(ReplaceError, "successor task changed"):
                    replace(args)

    def test_final_proof_rejects_live_pane_at_success_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_proof = replace_module.prove_committed_state

            def live_then_prove(**values: object) -> None:
                with patch.object(replace_module, "checked_pane_id", return_value="%9"):
                    original_proof(**values)  # type: ignore[arg-type]

            with patch.object(replace_module, "prove_committed_state", side_effect=live_then_prove), self.assertRaisesRegex(ReplaceError, "live at the final commit boundary"):
                replace(args)

    def test_final_proof_rejects_todo_namespace_rebind(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _stale, _todo, args = self.make_root(root)
                original_proof = replace_module.prove_committed_state

                def rebind_during_owner_proof(**values: object) -> None:
                    original_owners = replace_module.active_owners

                    def rebind_after_scan(*owner_args: object, **owner_kwargs: object):
                        owners = original_owners(*owner_args, **owner_kwargs)  # type: ignore[arg-type]
                        todo = root / "TODO.md"
                        data = b"changed\n" if changed else todo.read_bytes()
                        os.replace(todo, root / "TODO-owned")
                        todo.write_bytes(data)
                        return owners

                    with patch.object(replace_module, "active_owners", side_effect=rebind_after_scan):
                        original_proof(**values)  # type: ignore[arg-type]

                with patch.object(replace_module, "prove_committed_state", side_effect=rebind_during_owner_proof), self.assertRaisesRegex(ReplaceError, "namespace changed during the final commit proof"):
                    replace(args)

    def test_committed_recovery_final_proof_rejects_identical_or_changed_successor_rebind(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _stale, _todo, args = self.make_root(root)
                original_owners = replace_module.active_owners
                calls = 0

                def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise SystemExit("simulated crash")
                    return original_owners(scan_root, target, overrides, root_fd)

                with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                    replace(args)
                original_proof = replace_module.prove_committed_state

                def rebind_then_prove(**values: object) -> None:
                    successor = root / "hees_final_artifact.md"
                    data = b"changed\n" if changed else successor.read_bytes()
                    os.replace(successor, root / "recovery-successor-owned")
                    successor.write_bytes(data)
                    original_proof(**values)  # type: ignore[arg-type]

                with patch.object(replace_module, "prove_committed_state", side_effect=rebind_then_prove), self.assertRaisesRegex(ReplaceError, "successor task changed"):
                    replace(args)

    def test_committed_recovery_rejects_live_pane_at_success_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            original_owners = replace_module.active_owners
            calls = 0

            def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise SystemExit("simulated crash")
                return original_owners(scan_root, target, overrides, root_fd)

            with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                replace(args)
            original_proof = replace_module.prove_committed_state

            def live_then_prove(**values: object) -> None:
                with patch.object(replace_module, "checked_pane_id", return_value="%9"):
                    original_proof(**values)  # type: ignore[arg-type]

            with patch.object(replace_module, "prove_committed_state", side_effect=live_then_prove), self.assertRaisesRegex(ReplaceError, "live at the final commit boundary"):
                replace(args)

    def test_committed_recovery_rejects_todo_namespace_rebind(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _stale, _todo, args = self.make_root(root)
                original_owners = replace_module.active_owners
                calls = 0

                def interrupt_final_proof(scan_root: Path, target: str, overrides: dict[Path, bytes], root_fd: int | None = None):
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        raise SystemExit("simulated crash")
                    return original_owners(scan_root, target, overrides, root_fd)

                with patch.object(replace_module, "active_owners", side_effect=interrupt_final_proof), self.assertRaises(SystemExit):
                    replace(args)
                original_proof = replace_module.prove_committed_state

                def rebind_during_owner_proof(**values: object) -> None:
                    def rebind_after_scan(*owner_args: object, **owner_kwargs: object):
                        owners = original_owners(*owner_args, **owner_kwargs)  # type: ignore[arg-type]
                        todo = root / "TODO.md"
                        data = b"changed\n" if changed else todo.read_bytes()
                        os.replace(todo, root / "recovery-TODO-owned")
                        todo.write_bytes(data)
                        return owners

                    with patch.object(replace_module, "active_owners", side_effect=rebind_after_scan):
                        original_proof(**values)  # type: ignore[arg-type]

                with patch.object(replace_module, "prove_committed_state", side_effect=rebind_during_owner_proof), self.assertRaisesRegex(ReplaceError, "namespace changed during the final commit proof"):
                    replace(args)

    def test_failed_stage_restoration_is_not_reported_as_successful_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)
            real_exchange = replace_module.rename_exchange
            calls = 0

            def corrupt_then_fail_restore(parent_fd: int, left: str, right: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    os.replace(root / left, root / f"{left}.owned")
                    (root / left).write_bytes(b"foreign\n")
                    real_exchange(parent_fd, left, right)
                    return
                raise OSError("injected restoration failure")

            with patch.object(replace_module, "rename_exchange", side_effect=corrupt_then_fail_restore), self.assertRaisesRegex(ReplaceError, "rollback failed.*manual recovery required"):
                replace(args)

            self.assertTrue((root / replace_module.JOURNAL_NAME).is_file())

    def test_retained_displaced_receipts_are_canonical_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, _todo, args = self.make_root(root)

            _ = replace(args)

            journal = replace_module.read_journal(root / replace_module.JOURNAL_NAME, args)
            receipts = replace_module.validate_displaced_receipts(root, journal)
            self.assertEqual(2, len(receipts))
            self.assertEqual({"TODO.md", "hees_1170_policy.md"}, {replace_module.STAGE_RE.fullmatch(receipt.path.name).group(1) for receipt in receipts})

    def test_foreign_successor_race_preserves_committed_predecessor_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, todo_value, args = self.make_root(root)
            original_create = replace_module.create_successor

            def collide_successor(path: Path, data: bytes, mode: int, root_fd: int | None = None):
                if path.name == "hees_final_artifact.md":
                    path.write_text(task_text(managerat="guest_hees:9"), encoding="utf-8")
                    raise FileExistsError("injected successor race")
                return original_create(path, data, mode, root_fd)

            with patch.object(replace_module, "create_successor", side_effect=collide_successor), self.assertRaisesRegex(ReplaceError, "successor absence became unknown"):
                replace(args)
            self.assertEqual("done", metadata((root / "hees_1170_policy.md").read_text(encoding="utf-8"), root).status)
            self.assertNotEqual(todo_value, (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertTrue((root / replace_module.JOURNAL_NAME).is_file())
            self.assertTrue((root / "hees_final_artifact.md").is_file())

    def test_rollback_failure_is_explicit_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_text, todo_value, args = self.make_root(root)
            original_replace = replace_module.replace_existing

            def fail_commit_and_stale_rollback(expected: replace_module.Snapshot, data: bytes, root_fd: int | None = None):
                if expected.path.name == "TODO.md" and data != todo_value.encode():
                    raise OSError("injected commit failure")
                if expected.path.name == "hees_1170_policy.md" and data == stale_text.encode():
                    raise OSError("injected rollback failure")
                return original_replace(expected, data, root_fd)

            with patch.object(replace_module, "replace_existing", side_effect=fail_commit_and_stale_rollback), self.assertRaisesRegex(ReplaceError, "rollback failed.*manual recovery required"):
                replace(args)
            self.assertEqual(todo_value, (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertFalse((root / "hees_final_artifact.md").exists())
            self.assertEqual("done", metadata((root / "hees_1170_policy.md").read_text(encoding="utf-8"), root).status)

    def test_root_substitution_before_rollback_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _stale, todo_before, args = self.make_root(root)
            original_replace = replace_module.replace_existing
            original_root_check = replace_module.require_root_identity

            def fail_todo(expected: replace_module.Snapshot, data: bytes, root_fd: int | None = None):
                if expected.path.name == "TODO.md" and data != todo_before.encode():
                    raise OSError("injected forward failure")
                return original_replace(expected, data, root_fd)

            def reject_rollback(identity: replace_module.RootIdentity) -> None:
                stale_status = metadata((root / "hees_1170_policy.md").read_text(encoding="utf-8"), root).status
                if stale_status == "done":
                    raise ReplaceError("work-log root identity changed during replacement")
                original_root_check(identity)

            with (
                patch.object(replace_module, "replace_existing", side_effect=fail_todo),
                patch.object(replace_module, "require_root_identity", side_effect=reject_rollback),
                self.assertRaisesRegex(ReplaceError, "rollback failed.*manual recovery required"),
            ):
                replace(args)

    def test_post_restoration_identical_rebind_is_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale_before, todo_before, args = self.make_root(root)
            original_replace = replace_module.replace_existing
            rebound = False

            def fail_then_rebind(expected: replace_module.Snapshot, data: bytes, root_fd: int | None = None):
                nonlocal rebound
                if expected.path.name == "TODO.md" and data != todo_before.encode():
                    raise OSError("injected forward failure")
                restored = original_replace(expected, data, root_fd)
                if expected.path.name == "hees_1170_policy.md" and data == stale_before.encode() and not rebound:
                    rebound = True
                    os.replace(expected.path, expected.path.with_name("restored-owned"))
                    expected.path.write_bytes(data)
                return restored

            with patch.object(replace_module, "replace_existing", side_effect=fail_then_rebind), self.assertRaisesRegex(ReplaceError, "rollback failed.*manual recovery required"):
                replace(args)


if __name__ == "__main__":
    unittest.main()
