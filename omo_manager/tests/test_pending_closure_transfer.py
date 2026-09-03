from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_task_edit
from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_task_edit import Args
from omo_manager.omo_task_edit import parse_args
from omo_manager.omo_task_edit import run
from omo_manager.omo_task_edit import transfer_pending_items_for_closure


def task_text(
    runat: str,
    items: tuple[str, ...],
    *,
    status: str = "running",
    is_manager: bool = False,
    blocked_on: str = "",
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
        *([f"blocked_on: {blocked_on}"] if blocked_on else []),
        f"runat: {runat}",
        "tool: codex",
        "managerat: mgr:1",
        f"is_manager: {str(is_manager).lower()}",
    ]
    if items:
        lines.extend(("pending_task_items:", *(f"  - {item}" for item in items)))
    else:
        lines.append("pending_task_items: []")
    lines.extend(("---", f"body for {runat}"))
    return "\n".join(lines) + "\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PendingClosureTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.md"
        self.target = self.root / "target.md"
        self.authority = self.root / "manager_mail" / "1376.txt"
        self.authority_text = (
            "Subject: Close AMH agents\n\n"
            "tell those agents to document anything worth keeping long-term, move out their pending task items, then close them all\n"
        )
        self.source_original = task_text(
            "amh:2",
            ("first source request", "second source request (manager_mail/1376.txt)"),
            status="blocked",
            blocked_on="human-authorized AMH shutdown",
        )
        self.target_original = task_text(
            "keep:1",
            ("existing destination request",),
            status="long_running",
            is_manager=True,
            blocked_on="persistent manager role",
        )
        self.source.write_text(self.source_original, encoding="utf-8")
        self.target.write_text(self.target_original, encoding="utf-8")
        self.authority.parent.mkdir()
        self.authority.write_text(self.authority_text, encoding="utf-8")
        self.authority.chmod(0o600)
        (self.root / "TODO.md").write_text("current:\nsource.md amh:2\ntarget.md keep:1\n", encoding="utf-8")

    def args(self, *, source_sha256: str | None = None, destination_sha256: str | None = None) -> Args:
        return Args(
            self.root,
            None,
            "pending-closure-transfer",
            source_file=Path("source.md"),
            target_file=Path("target.md"),
            source_sha256=source_sha256 or digest(self.source_original),
            destination_sha256=destination_sha256 or digest(self.target_original),
            authority_file=Path("manager_mail/1376.txt"),
            authority_sha256=digest(self.authority_text),
        )

    def test_transfers_complete_queue_in_order_with_provenance_and_blocker(self) -> None:
        self.assertEqual(0, run(self.args()))

        source_metadata = parse_task_metadata(self.source.read_text(encoding="utf-8"), self.root)
        target_metadata = parse_task_metadata(self.target.read_text(encoding="utf-8"), self.root)
        assert source_metadata is not None
        assert target_metadata is not None
        self.assertEqual((), source_metadata.pending_task_items)
        self.assertEqual(
            (
                "existing destination request",
                "first source request",
                "second source request (manager_mail/1376.txt)",
            ),
            target_metadata.pending_task_items,
        )
        self.assertIn('"direction":"sent"', self.source.read_text(encoding="utf-8"))
        target_text_value = self.target.read_text(encoding="utf-8")
        self.assertIn('"direction":"received"', target_text_value)
        self.assertIn('"source_blocked_on":"human-authorized AMH shutdown"', target_text_value)
        self.assertIn(digest(self.source_original), target_text_value)

    def test_requires_digest_bound_cli_arguments(self) -> None:
        args = parse_args(
            [
                "--root",
                str(self.root),
                "pending-closure-transfer",
                "--from",
                "source.md",
                "--to",
                "target.md",
                "--source-sha256",
                digest(self.source_original),
                "--destination-sha256",
                digest(self.target_original),
                "--authority-file",
                "manager_mail/1376.txt",
                "--authority-sha256",
                digest(self.authority_text),
            ]
        )

        self.assertEqual("pending-closure-transfer", args.command)
        self.assertEqual(digest(self.source_original), args.source_sha256)
        self.assertEqual(digest(self.target_original), args.destination_sha256)
        self.assertEqual(digest(self.authority_text), args.authority_sha256)

    def test_concurrent_source_change_fails_closed_without_overwrite(self) -> None:
        real_replace = omo_task_edit.replace_if_unchanged_locked
        concurrent = self.source_original + "external concurrent note\n"
        n_calls = 0

        def race(path: Path, text: str, before: object) -> None:
            nonlocal n_calls
            n_calls += 1
            if n_calls == 1:
                self.source.write_text(concurrent, encoding="utf-8")
            real_replace(path, text, before)  # type: ignore[arg-type]

        with patch.object(omo_task_edit, "replace_if_unchanged_locked", side_effect=race), redirect_stderr(io.StringIO()):
            self.assertEqual(2, run(self.args()))

        self.assertEqual(concurrent, self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.target_original, self.target.read_text(encoding="utf-8"))

    def test_duplicate_item_owner_is_refused_without_changes(self) -> None:
        duplicate = self.root / "duplicate.md"
        duplicate_text = task_text("other:3", ("first source request",))
        duplicate.write_text(duplicate_text, encoding="utf-8")

        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, run(self.args()))

        self.assertEqual(self.source_original, self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.target_original, self.target.read_text(encoding="utf-8"))
        self.assertEqual(duplicate_text, duplicate.read_text(encoding="utf-8"))

    def test_second_replacement_failure_rolls_destination_back(self) -> None:
        real_replace = omo_task_edit.replace_if_unchanged_locked
        n_calls = 0

        def fail_destination(path: Path, text: str, before: object) -> None:
            nonlocal n_calls
            n_calls += 1
            if n_calls == 2:
                raise OSError("injected destination failure")
            real_replace(path, text, before)  # type: ignore[arg-type]

        with patch.object(omo_task_edit, "replace_if_unchanged_locked", side_effect=fail_destination), redirect_stderr(io.StringIO()):
            self.assertEqual(2, run(self.args()))

        self.assertEqual(3, n_calls)
        self.assertEqual(self.source_original, self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.target_original, self.target.read_text(encoding="utf-8"))

    def test_interrupted_transfer_recovers_from_durable_record(self) -> None:
        real_replace = omo_task_edit.replace_if_unchanged_locked
        n_calls = 0

        def interrupt_second(path: Path, text: str, before: object) -> None:
            nonlocal n_calls
            n_calls += 1
            if n_calls == 2:
                raise KeyboardInterrupt
            real_replace(path, text, before)  # type: ignore[arg-type]

        with patch.object(omo_task_edit, "replace_if_unchanged_locked", side_effect=interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                _ = transfer_pending_items_for_closure(
                    self.root,
                    self.source,
                    self.target,
                    digest(self.source_original),
                    digest(self.target_original),
                    self.authority,
                    digest(self.authority_text),
                )

        self.assertTrue((self.root / ".omo-pending-closure-transfer.json").is_file())
        self.assertEqual(self.source_original, self.source.read_text(encoding="utf-8"))
        self.assertNotEqual(self.target_original, self.target.read_text(encoding="utf-8"))
        self.assertEqual(0, run(self.args()))
        self.assertFalse((self.root / ".omo-pending-closure-transfer.json").exists())
        source_metadata = parse_task_metadata(self.source.read_text(encoding="utf-8"), self.root)
        target_metadata = parse_task_metadata(self.target.read_text(encoding="utf-8"), self.root)
        assert source_metadata is not None
        assert target_metadata is not None
        self.assertEqual((), source_metadata.pending_task_items)
        self.assertEqual(
            ("existing destination request", "first source request", "second source request (manager_mail/1376.txt)"),
            target_metadata.pending_task_items,
        )

    def test_post_preflight_todo_change_rolls_back_task_writes(self) -> None:
        real_replace = omo_task_edit.replace_if_unchanged_locked
        changed_todo = "current:\nsource.md amh:2\ntarget.md keep:1\nother.md other:1\n"
        n_calls = 0

        def race_todo(path: Path, text: str, before: object) -> None:
            nonlocal n_calls
            n_calls += 1
            real_replace(path, text, before)  # type: ignore[arg-type]
            if n_calls == 2:
                (self.root / "TODO.md").write_text(changed_todo, encoding="utf-8")

        with patch.object(omo_task_edit, "replace_if_unchanged_locked", side_effect=race_todo), redirect_stderr(io.StringIO()):
            self.assertEqual(2, run(self.args()))

        self.assertEqual(self.source_original, self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.target_original, self.target.read_text(encoding="utf-8"))
        self.assertEqual(changed_todo, (self.root / "TODO.md").read_text(encoding="utf-8"))

    def test_post_preflight_unlinked_task_creation_rolls_back(self) -> None:
        real_replace = omo_task_edit.replace_if_unchanged_locked
        created = self.root / "created.md"
        created_text = task_text("created:1", ("first source request",))
        n_calls = 0

        def create_unlinked_task(path: Path, text: str, before: object) -> None:
            nonlocal n_calls
            n_calls += 1
            real_replace(path, text, before)  # type: ignore[arg-type]
            if n_calls == 2:
                created.write_text(created_text, encoding="utf-8")

        with patch.object(omo_task_edit, "replace_if_unchanged_locked", side_effect=create_unlinked_task), redirect_stderr(io.StringIO()):
            self.assertEqual(2, run(self.args()))

        self.assertEqual(self.source_original, self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.target_original, self.target.read_text(encoding="utf-8"))
        self.assertEqual(created_text, created.read_text(encoding="utf-8"))

    def test_refuses_wrong_digest_non_amh_source_and_unsafe_destination(self) -> None:
        cases = (
            ("digest", self.source_original, self.target_original, "0" * 64, digest(self.target_original)),
            (
                "source",
                self.source_original.replace("runat: amh:2", "runat: worker:2"),
                self.target_original,
                None,
                None,
            ),
            (
                "not blocked",
                self.source_original.replace("status: blocked\nblocked_on: human-authorized AMH shutdown\n", "status: running\n"),
                self.target_original,
                None,
                None,
            ),
            (
                "destination",
                self.source_original,
                self.target_original.replace("runat: keep:1", "runat: hkeep:1"),
                None,
                None,
            ),
            (
                "manager",
                self.source_original,
                self.target_original.replace("is_manager: true", "is_manager: false"),
                None,
                None,
            ),
        )
        for label, source_text_value, target_text_value, source_sha256, destination_sha256 in cases:
            with self.subTest(label=label):
                self.source.write_text(source_text_value, encoding="utf-8")
                self.target.write_text(target_text_value, encoding="utf-8")
                source_digest = source_sha256 or digest(source_text_value)
                target_digest = destination_sha256 or digest(target_text_value)
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(self.args(source_sha256=source_digest, destination_sha256=target_digest)))
                self.assertEqual(source_text_value, self.source.read_text(encoding="utf-8"))
                self.assertEqual(target_text_value, self.target.read_text(encoding="utf-8"))

    def test_refuses_non_authoritative_shutdown_file(self) -> None:
        unsafe_authority = self.authority_text.replace("move out their pending task items", "leave their pending task items")
        self.authority.write_text(unsafe_authority, encoding="utf-8")
        args = Args(
            self.root,
            None,
            "pending-closure-transfer",
            source_file=Path("source.md"),
            target_file=Path("target.md"),
            source_sha256=digest(self.source_original),
            destination_sha256=digest(self.target_original),
            authority_file=Path("manager_mail/1376.txt"),
            authority_sha256=digest(unsafe_authority),
        )

        with redirect_stderr(io.StringIO()):
            self.assertEqual(2, run(args))

        self.assertEqual(self.source_original, self.source.read_text(encoding="utf-8"))
        self.assertEqual(self.target_original, self.target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
