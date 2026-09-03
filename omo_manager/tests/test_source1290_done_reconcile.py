from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

from omo_manager.omo_source1290_done_reconcile import (
    ARCHIVE_TODO,
    ARCHIVED_MEMORY,
    ARCHIVED_INTERRUPTED_EVAL,
    ARCHIVED_INTERRUPTED_FIX,
    ARCHIVED_SOURCE1290_AUTHORITY,
    ARCHIVED_TRANSCRIPTION,
    CANONICAL_CARRIER,
    DUPLICATE_CARRIER,
    Args,
    SOURCE1290_AUDIT_SHA256,
    SOURCE1290_EXCERPT,
    parse_args,
    reconcile,
    source1290_authority,
    validate_completed_audit,
)
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import Args as StatusArgs
from omo_manager.omo_task_status import reconcile_todo_text, recover_exited_shell_done

PANE_ID = "%42"
SESSION_ID = "11111111-2222-3333-4444-555555555555"
REPORT_TOKEN = "accepted-report-token"
CAPTURE_SHA256 = "e" * 64
CARRIER_TARGET = "vlcontext_recovery:2"
# 🧑 Human Source `manager_mail/85c5dff58359-1290.txt:3-4`: “Close the ‘memory’ thing. It is so old.”
REAL_FIXTURE = Path(__file__).parent / "fixtures" / "source1290"
REAL_FIXTURE_SHA256 = {
    "completed-audit.yaml": "eafa5c27d35ea2dacb4c94a0c53619f06acfb66bef703bf63dc569ac7af5fedf",
    "post-archive/TODO.md": "f042a22a2feab3649dcc1798204f2c7193cd083ffbc404c2aec984c4a2f0191f",
    "post-archive/mem1290_auth.md": "86e0cbe819e7b1d0f2899d35b903744209222d9eaa46ca8e6929bb63af1ec30a",
    "post-archive/memory_auth_1290.md": "3a0291e6ea4c6aa8ef59055d65e97c53a8468d1a29d6e41c9aad7e760f59c811",
    "post-archive/202608/old_todos.md": "5203a0a09617a417543abd121eac39efc44fb6ddef101366ae7de6042df29c83",
    "post-archive/202608/memory_research_mgr.md": "d2ae03a9e19f981ec43c6b8527fca1475a31a7c0593611c8ac6f36dbb392e705",
    "post-archive/202608/transcription_sw.md": "a01fec08cfdcab16755a5d44c5ae78fde5110b05967ed7b0c324bba55cc6bea1",
    "post-archive/202608/mem1290_eval.md": "62d641ddcaede3417b5bb024c676d0c3322f8d3bdbac31aa03b9e269259a19cf",
    "post-archive/202608/mem1290_fix.md": "ee0429ecf458721f24d4965e285dd59ade51742359b717fd1d697067826d35d5",
    "post-archive/202608/manager_mail/85c5dff58359-1290.txt.b64": "0b33b7c6fd2e90680eec166668fdf2abcdc7cc45b94bd660974d0bdb1995e169",
}


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def task_text(
    *,
    status: str,
    blocker: str = "",
    target: str,
    manager: bool,
    items: tuple[str, ...] = (),
    body: str = "",
) -> str:
    pending = "pending_task_items: []\n" if not items else "pending_task_items:\n" + "".join(f"  - {item}\n" for item in items)
    blocked = f"blocked_on: {blocker}\n" if blocker else ""
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocked}runat: {target}\ntool: codex\nmanagerat: cfg:0\nis_manager: {str(manager).lower()}\n{pending}---\n{body}"


class Source1290DoneReconcileTests(unittest.TestCase):
    def test_real_audit_and_post_archive_fixture_matches_source_evidence(self) -> None:
        for relative, expected_sha256 in REAL_FIXTURE_SHA256.items():
            payload = (REAL_FIXTURE / relative).read_bytes()
            if relative.endswith(".b64"):
                payload = base64.b64decode(payload)
            self.assertEqual(expected_sha256, sha256(payload), relative)

        with tempfile.TemporaryDirectory() as tmp:
            audit = validate_completed_audit(
                (REAL_FIXTURE / "completed-audit.yaml").read_bytes(),
                "mem1290_auth.md",
                Path(tmp).resolve(),
            )
        self.assertEqual("ccb6a488394d01ac3687738b8117e46c6daaa5f9675bd776c9f7f48c3a3a1429", audit.memory_sha256)
        self.assertEqual("cebfc885684e2281c15026a543472c1ac40b533f4343132dde805ad58c26af95", audit.transcription_sha256)

    def fixture(self, base: Path) -> tuple[Args, Path, Path, Path, Path]:
        root = base.resolve()
        _ = shutil.copytree(REAL_FIXTURE / "post-archive", root, dirs_exist_ok=True)
        encoded_authority = root / f"{ARCHIVED_SOURCE1290_AUTHORITY.partition(':')[0]}.b64"
        authority = encoded_authority.with_suffix("")
        authority.write_bytes(base64.b64decode(encoded_authority.read_bytes()))
        encoded_authority.unlink()
        authority.chmod(0o600)
        carrier = root / CANONICAL_CARRIER
        todo = root / "TODO.md"
        audit_directory = root / "private"
        audit_directory.mkdir(mode=0o700)
        audit = audit_directory / "cancel.yaml"
        audit.write_bytes((REAL_FIXTURE / "completed-audit.yaml").read_bytes())
        audit.chmod(0o600)
        args = Args(
            root,
            CANONICAL_CARRIER,
            sha256(carrier.read_bytes()),
            sha256(todo.read_bytes()),
            sha256((root / ARCHIVE_TODO).read_bytes()),
            PANE_ID,
            SESSION_ID,
            REPORT_TOKEN,
            audit.resolve(),
            sha256(audit.read_bytes()),
        )
        return args, carrier, todo, authority, audit

    def test_mailbox_source_rejects_parent_directory_rebind_during_authenticated_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            work = base / "work"
            work.mkdir()
            args, _carrier, _todo, authority, _audit = self.fixture(work)
            root = args.root
            payload = authority.read_bytes()
            expected_sha256 = sha256(payload)
            root_before = os.stat(root)
            month_before = os.stat(root / "202608")
            original_stat = os.stat
            rebound = False

            def stat_with_parent_rebind(path: os.PathLike[str] | str | int, *stat_args: object, **stat_kwargs: object) -> os.stat_result:
                nonlocal rebound
                if path == "manager_mail" and stat_kwargs.get("dir_fd") is not None and not rebound:
                    rebound = True
                    os.rename(root / "202608" / "manager_mail", base / "displaced-manager-mail")
                    os.mkdir(root / "202608" / "manager_mail", 0o755)
                    descriptor = os.open(root / "202608" / "manager_mail" / authority.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    try:
                        _ = os.write(descriptor, payload)
                    finally:
                        os.close(descriptor)
                    os.utime(root / "202608", ns=(month_before.st_atime_ns, month_before.st_mtime_ns))
                    os.utime(root, ns=(root_before.st_atime_ns, root_before.st_mtime_ns))
                return original_stat(path, *stat_args, **stat_kwargs)  # type: ignore[arg-type]

            with (
                patch("omo_manager.omo_source1290_done_reconcile.os.stat", side_effect=stat_with_parent_rebind),
                self.assertRaisesRegex(OSError, "mailbox identity"),
            ):
                source1290_authority(root, expected_sha256)
            self.assertTrue(rebound)

    def test_finishes_only_carrier_and_never_enters_completion_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, authority, audit = self.fixture(Path(tmp))
            authority_before = authority.read_bytes()
            audit_before = audit.read_bytes()
            todo_before = todo.read_text(encoding="utf-8")
            protected = tuple(
                args.root / relative
                for relative in (ARCHIVE_TODO, ARCHIVED_MEMORY, ARCHIVED_TRANSCRIPTION, DUPLICATE_CARRIER, ARCHIVED_INTERRUPTED_EVAL, ARCHIVED_INTERRUPTED_FIX)
            )
            protected_before = tuple(item.read_bytes() for item in protected)
            live = True

            def pane(_target: str) -> str:
                return PANE_ID if live else ""

            def close(
                _target: str,
                _pane: str,
                _session: str,
                _evidence: str,
                **kwargs: object,
            ) -> None:
                nonlocal live
                identity = kwargs["evidence_is_current"]
                assert callable(identity)
                self.assertTrue(identity())
                self.assertEqual(CAPTURE_SHA256, kwargs["expected_capture_sha256"])
                live = False

            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=pane),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell", side_effect=close) as shell_close,
                patch("omo_manager.omo_task_status.require_owner_completion") as completion_gate,
                patch("omo_manager.omo_completion_email.send_completion_email") as email,
            ):
                reconcile(args)

            metadata = parse_task_metadata(carrier.read_text(encoding="utf-8"), args.root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual((), metadata.pending_task_items)
            self.assertIn(SOURCE1290_EXCERPT, carrier.read_text(encoding="utf-8"))
            self.assertIn(f"session_id: `{SESSION_ID}`", carrier.read_text(encoding="utf-8"))
            self.assertEqual(
                reconcile_todo_text(args.root, carrier, todo_before, CARRIER_TARGET, "previous", ("current",)),
                todo.read_text(encoding="utf-8"),
            )
            self.assertEqual(authority_before, authority.read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())
            self.assertEqual(protected_before, tuple(item.read_bytes() for item in protected))
            self.assertEqual(CARRIER_TARGET, shell_close.call_args.args[0])
            self.assertEqual(PANE_ID, shell_close.call_args.args[1])
            completion_gate.assert_not_called()
            email.assert_not_called()

    def test_close_failure_leaves_exact_existing_recovery_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, authority, audit = self.fixture(Path(tmp))
            todo_before = todo.read_bytes()
            authority_before = authority.read_bytes()
            audit_before = audit.read_bytes()
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=RuntimeError("pane evidence changed"),
                ),
                self.assertRaisesRegex(TaskFrontmatterError, "existing done_close_failed recovery state"),
            ):
                reconcile(args)

            failed_text = carrier.read_text(encoding="utf-8")
            blocker = f"done_close_failed: target is not a supported live Codex pane: {PANE_ID} status=not_codex"
            self.assertIn(f"status: blocked\nblocked_on: {blocker}\n", failed_text)
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertEqual(authority_before, authority.read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())

            recovery_args = StatusArgs(
                args.root,
                args.task_file,
                "done",
                "",
                session_id=SESSION_ID,
                recover_exited_shell_done=True,
                pane_id=PANE_ID,
                terminal_evidence=REPORT_TOKEN,
            )
            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=PANE_ID),
                patch("omo_manager.omo_task_status.close_exited_codex_shell"),
            ):
                target, session = recover_exited_shell_done(
                    recovery_args,
                    carrier,
                    failed_text,
                    carrier.stat(),
                )
            self.assertEqual((CARRIER_TARGET, SESSION_ID), (target, session))
            recovered = parse_task_metadata(carrier.read_text(encoding="utf-8"), args.root)
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual("done", recovered.status)

    def test_does_not_close_before_failed_handoff_directory_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, authority, audit = self.fixture(Path(tmp))
            todo_before = todo.read_bytes()
            authority_before = authority.read_bytes()
            audit_before = audit.read_bytes()
            close_intent = audit.with_name(f"{audit.name}.source1290-carrier-close-intent")
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.fsync_bound_directory",
                    side_effect=OSError("durability boundary unavailable"),
                ) as durable,
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                self.assertRaisesRegex(OSError, "durability boundary unavailable"),
            ):
                reconcile(args)
            durable.assert_called_once_with(carrier.parent)
            close.assert_not_called()
            self.assertIn("status: blocked\nblocked_on: done_close_failed:", carrier.read_text(encoding="utf-8"))
            self.assertFalse(close_intent.exists())
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertEqual(authority_before, authority.read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())

    def test_retry_finishes_after_successful_close_then_pre_note_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, authority, audit = self.fixture(Path(tmp))
            authority_before = authority.read_bytes()
            audit_before = audit.read_bytes()
            todo_before = todo.read_text(encoding="utf-8")
            live = True

            def pane(_target: str) -> str:
                return PANE_ID if live else ""

            def successful_close(
                _target: str,
                _pane: str,
                _session: str,
                _evidence: str,
                **kwargs: object,
            ) -> None:
                nonlocal live
                identity = kwargs["evidence_is_current"]
                assert callable(identity)
                self.assertTrue(identity())
                live = False

            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=pane),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=successful_close,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_note",
                    side_effect=KeyboardInterrupt("process died before close note"),
                ),
                self.assertRaisesRegex(KeyboardInterrupt, "before close note"),
            ):
                reconcile(args)

            interrupted = carrier.read_text(encoding="utf-8")
            self.assertIn("status: blocked\nblocked_on: done_close_failed:", interrupted)
            recovery = audit.with_name(f"{audit.name}.source1290-carrier-close-intent")
            self.assertTrue(recovery.is_file())
            self.assertEqual(0o600, recovery.stat().st_mode & 0o777)
            recovery_before = recovery.read_bytes()

            retry = replace(args, task_sha256=sha256(carrier.read_bytes()))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=pane),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_note",
                    side_effect=KeyboardInterrupt("retry died before close note"),
                ),
                self.assertRaisesRegex(KeyboardInterrupt, "retry died"),
            ):
                reconcile(retry)
            self.assertEqual(interrupted, carrier.read_text(encoding="utf-8"))
            self.assertEqual(recovery_before, recovery.read_bytes())

            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=pane),
                patch("omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell") as validate,
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                patch("omo_manager.omo_task_status.require_owner_completion") as completion_gate,
                patch("omo_manager.omo_completion_email.send_completion_email") as email,
            ):
                reconcile(retry)
            validate.assert_not_called()
            close.assert_not_called()
            completion_gate.assert_not_called()
            email.assert_not_called()
            metadata = parse_task_metadata(carrier.read_text(encoding="utf-8"), args.root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual(authority_before, authority.read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())
            self.assertEqual(recovery_before, recovery.read_bytes())
            self.assertEqual(
                reconcile_todo_text(args.root, carrier, todo_before, CARRIER_TARGET, "previous", ("current",)),
                todo.read_text(encoding="utf-8"),
            )
            self.assertIn(f"session_id: `{SESSION_ID}`", carrier.read_text(encoding="utf-8"))

    def test_absent_pane_finish_rejects_target_reappearance_before_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, _authority, audit = self.fixture(Path(tmp))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=RuntimeError("close rejected after intent"),
                ),
                self.assertRaisesRegex(TaskFrontmatterError, "existing done_close_failed recovery state"),
            ):
                reconcile(args)
            failed = carrier.read_bytes()
            todo_before = todo.read_bytes()
            retry = replace(args, task_sha256=sha256(failed))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=("", "", PANE_ID)),
                patch("omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell") as validate,
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                patch("omo_manager.omo_source1290_done_reconcile.close_note") as note,
                self.assertRaisesRegex(OSError, "target reappeared"),
            ):
                reconcile(retry)
            validate.assert_not_called()
            close.assert_not_called()
            note.assert_not_called()
            self.assertEqual(failed, carrier.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertTrue(audit.with_name(f"{audit.name}.source1290-carrier-close-intent").is_file())

    def test_absent_pane_finish_rechecks_bound_evidence_before_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, _authority, audit = self.fixture(Path(tmp))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=RuntimeError("close rejected after intent"),
                ),
                self.assertRaisesRegex(TaskFrontmatterError, "existing done_close_failed recovery state"),
            ):
                reconcile(args)
            failed = carrier.read_bytes()
            todo_before = todo.read_bytes()
            retry = replace(args, task_sha256=sha256(failed))
            pane_checks = 0

            def absent_then_audit_drift(_target: str) -> str:
                nonlocal pane_checks
                pane_checks += 1
                if pane_checks == 3:
                    audit.write_bytes(audit.read_bytes() + b"drift: true\n")
                return ""

            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=absent_then_audit_drift),
                patch("omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell") as validate,
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                patch("omo_manager.omo_source1290_done_reconcile.close_note") as note,
                self.assertRaisesRegex(OSError, "bound lifecycle evidence changed"),
            ):
                reconcile(retry)
            validate.assert_not_called()
            close.assert_not_called()
            note.assert_not_called()
            self.assertEqual(failed, carrier.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())

    def test_retry_does_not_infer_close_from_an_absent_pane_without_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, _authority, audit = self.fixture(Path(tmp))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=RuntimeError("close rejected after intent"),
                ),
                self.assertRaisesRegex(TaskFrontmatterError, "existing done_close_failed recovery state"),
            ):
                reconcile(args)
            failed = carrier.read_bytes()
            todo_before = todo.read_bytes()
            audit.with_name(f"{audit.name}.source1290-carrier-close-intent").unlink()
            retry = replace(args, task_sha256=sha256(failed))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=""),
                patch("omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell") as validate,
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                self.assertRaisesRegex(OSError, "absent without an evidence-bound close intent"),
            ):
                reconcile(retry)
            validate.assert_not_called()
            close.assert_not_called()
            self.assertEqual(failed, carrier.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())

    def test_live_retry_rejects_terminal_capture_drift_from_close_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, _authority, _audit = self.fixture(Path(tmp))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=RuntimeError("close rejected"),
                ),
                self.assertRaises(TaskFrontmatterError),
            ):
                reconcile(args)
            failed = carrier.read_bytes()
            todo_before = todo.read_bytes()
            retry = replace(args, task_sha256=sha256(failed))
            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value="f" * 64,
                ),
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                self.assertRaisesRegex(OSError, "capture drifted"),
            ):
                reconcile(retry)
            close.assert_not_called()
            self.assertEqual(failed, carrier.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())

    def test_rejects_task_audit_authority_membership_and_pane_drift_before_handoff(self) -> None:
        cases = (
            "task",
            "todo",
            "archive_todo",
            "audit",
            "envelope_hash",
            "authority",
            "memory",
            "transcription",
            "duplicate",
            "interrupted",
            "prearchive",
            "owner",
            "archived_owner",
            "pane",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                args, carrier, todo, authority, audit = self.fixture(Path(tmp))
                original = carrier.read_bytes()
                if case == "task":
                    changed = carrier.read_text(encoding="utf-8").replace("pending_task_items: []", "pending_task_items:\n  - open")
                    carrier.write_text(changed, encoding="utf-8")
                    args = replace(args, task_sha256=sha256(carrier.read_bytes()))
                elif case == "todo":
                    todo.write_text(todo.read_text(encoding="utf-8").replace("current:\n", "current:\n202608/memory_research_mgr.md wl:32\n", 1), encoding="utf-8")
                    args = replace(args, todo_sha256=sha256(todo.read_bytes()))
                elif case == "archive_todo":
                    archive_todo = args.root / ARCHIVE_TODO
                    archive_todo.write_text(archive_todo.read_text(encoding="utf-8").replace("memory_research_mgr.md wl:32", "memory_research_mgr.md wl:31"), encoding="utf-8")
                    args = replace(args, archive_todo_sha256=sha256(archive_todo.read_bytes()))
                elif case == "audit":
                    audit.write_bytes(audit.read_bytes().replace(b"final-result: success", b"final-result: prepared"))
                    args = replace(args, completed_audit_sha256=sha256(audit.read_bytes()))
                elif case == "envelope_hash":
                    content = yaml.safe_load(audit.read_bytes())
                    content["authority_envelope_sha256"] = "d" * 64
                    audit.write_bytes(yaml.safe_dump(content, sort_keys=False).encode())
                    args = replace(args, completed_audit_sha256=sha256(audit.read_bytes()))
                elif case == "authority":
                    authority.write_text("Subject: memory cancellation\n\nchanged\n", encoding="utf-8")
                elif case in {"memory", "transcription"}:
                    relative = {"memory": ARCHIVED_MEMORY, "transcription": ARCHIVED_TRANSCRIPTION}[case]
                    changed = args.root / relative
                    changed.write_bytes(changed.read_bytes() + b"drift\n")
                elif case == "duplicate":
                    changed = args.root / DUPLICATE_CARRIER
                    changed.write_text(changed.read_text(encoding="utf-8").replace("Close the “memory” thing.", "Change the memory thing."), encoding="utf-8")
                elif case == "interrupted":
                    changed = args.root / ARCHIVED_INTERRUPTED_EVAL
                    changed.write_text(changed.read_text(encoding="utf-8").replace("runat: vldr:2", "runat: vldr:9"), encoding="utf-8")
                elif case == "prearchive":
                    (args.root / "memory_research_mgr.md").write_text(task_text(status="done", target="wl:32", manager=True), encoding="utf-8")
                elif case == "owner":
                    (args.root / "other.md").write_text(
                        task_text(status="running", target=CARRIER_TARGET, manager=False),
                        encoding="utf-8",
                    )
                elif case == "archived_owner":
                    (args.root / "other.md").write_text(
                        task_text(status="running", target="vldr:2", manager=False),
                        encoding="utf-8",
                    )
                with (
                    patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value="" if case == "pane" else PANE_ID),
                    patch("omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell") as validate,
                    patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                    self.assertRaises((OSError, TaskFrontmatterError)),
                ):
                    reconcile(args)
                close.assert_not_called()
                if case not in {"task"}:
                    self.assertEqual(original, carrier.read_bytes())
                validate.assert_not_called()

    def test_rejects_evidence_drift_after_shell_authentication_without_handoff(self) -> None:
        for case in ("task", "todo", "archive_todo", "audit", "authority", "memory", "transcription", "interrupted", "membership", "pane"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                args, carrier, todo, authority, audit = self.fixture(Path(tmp))
                original = carrier.read_bytes()
                pane_calls = 0

                def pane(_target: str) -> str:
                    nonlocal pane_calls
                    pane_calls += 1
                    return "" if case == "pane" and pane_calls > 1 else PANE_ID

                def drift(*_args: object) -> None:
                    if case == "task":
                        carrier.write_bytes(original + b"concurrent evidence\n")
                    elif case == "todo":
                        todo.write_bytes(todo.read_bytes() + b"drift\n")
                    elif case == "archive_todo":
                        archive_todo = args.root / ARCHIVE_TODO
                        archive_todo.write_bytes(archive_todo.read_bytes() + b"drift\n")
                    elif case == "audit":
                        audit.write_bytes(audit.read_bytes() + b"drift: true\n")
                    elif case == "authority":
                        authority.write_bytes(authority.read_bytes() + b"drift\n")
                    elif case in {"memory", "transcription", "interrupted"}:
                        relative = {"memory": ARCHIVED_MEMORY, "transcription": ARCHIVED_TRANSCRIPTION, "interrupted": ARCHIVED_INTERRUPTED_FIX}[case]
                        changed = args.root / relative
                        changed.write_bytes(changed.read_bytes() + b"drift\n")
                    elif case == "membership":
                        (args.root / "late.md").write_text(task_text(status="running", target="cfg:9", manager=False), encoding="utf-8")

                with (
                    patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", side_effect=pane),
                    patch("omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell", side_effect=drift),
                    patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                    self.assertRaises((OSError, TaskFrontmatterError)),
                ):
                    reconcile(args)
                close.assert_not_called()
                self.assertNotIn(b"done_close_failed", carrier.read_bytes())

    def test_rejects_duplicate_owner_drift_during_shell_authentication_without_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, authority, audit = self.fixture(Path(tmp))
            duplicate = args.root / DUPLICATE_CARRIER
            carrier_before = carrier.read_bytes()
            todo_before = todo.read_bytes()
            authority_before = authority.read_bytes()
            audit_before = audit.read_bytes()

            def add_duplicate_owner(*_args: object) -> str:
                duplicate.write_text(
                    duplicate.read_text(encoding="utf-8").replace("runat: agent_managers:78", f"runat: {CARRIER_TARGET}"),
                    encoding="utf-8",
                )
                return CAPTURE_SHA256

            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    side_effect=add_duplicate_owner,
                ),
                patch("omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell") as close,
                patch("omo_manager.omo_task_status.require_owner_completion") as completion_gate,
                patch("omo_manager.omo_completion_email.send_completion_email") as email,
                self.assertRaisesRegex(OSError, "after shell authentication"),
            ):
                reconcile(args)
            close.assert_not_called()
            completion_gate.assert_not_called()
            email.assert_not_called()
            self.assertEqual(carrier_before, carrier.read_bytes())
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertEqual(authority_before, authority.read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())
            self.assertFalse(audit.with_name(f"{audit.name}.source1290-carrier-close-intent").exists())
            metadata = parse_task_metadata(carrier.read_text(encoding="utf-8"), args.root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done_close_in_progress: manager is closing the agent before marking done", metadata.blocked_on)

    def test_final_pre_close_gate_rejects_duplicate_owner_drift_before_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, carrier, todo, authority, audit = self.fixture(Path(tmp))
            duplicate = args.root / DUPLICATE_CARRIER
            todo_before = todo.read_bytes()
            authority_before = authority.read_bytes()
            audit_before = audit.read_bytes()

            def reject_owner_drift(
                _target: str,
                _pane: str,
                _session: str,
                _evidence: str,
                **kwargs: object,
            ) -> None:
                duplicate.write_text(
                    duplicate.read_text(encoding="utf-8").replace("runat: agent_managers:78", f"runat: {CARRIER_TARGET}"),
                    encoding="utf-8",
                )
                evidence_is_current = kwargs["evidence_is_current"]
                assert callable(evidence_is_current)
                self.assertFalse(evidence_is_current())
                raise RuntimeError("final evidence gate rejected owner drift")

            with (
                patch("omo_manager.omo_source1290_done_reconcile.exact_pane_id", return_value=PANE_ID),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.validate_exited_codex_shell",
                    return_value=CAPTURE_SHA256,
                ),
                patch(
                    "omo_manager.omo_source1290_done_reconcile.close_exited_codex_shell",
                    side_effect=reject_owner_drift,
                ),
                self.assertRaisesRegex(TaskFrontmatterError, "existing done_close_failed recovery state"),
            ):
                reconcile(args)
            self.assertIn("status: blocked\nblocked_on: done_close_failed:", carrier.read_text(encoding="utf-8"))
            self.assertEqual(todo_before, todo.read_bytes())
            self.assertEqual(authority_before, authority.read_bytes())
            self.assertEqual(audit_before, audit.read_bytes())
            self.assertTrue(audit.with_name(f"{audit.name}.source1290-carrier-close-intent").is_file())

    def test_parse_requires_complete_exact_evidence(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--root", "/tmp/work"])
        args = parse_args(
            [
                "--root",
                "/tmp/work",
                "--task-file",
                "carrier.md",
                "--task-sha256",
                "a" * 64,
                "--todo-sha256",
                "b" * 64,
                "--archive-todo-sha256",
                "d" * 64,
                "--pane-id",
                PANE_ID,
                "--session-id",
                SESSION_ID,
                "--terminal-evidence",
                REPORT_TOKEN,
                "--completed-audit",
                "/tmp/private/audit.yaml",
                "--completed-audit-sha256",
                SOURCE1290_AUDIT_SHA256,
            ]
        )
        self.assertEqual(Path("carrier.md"), args.task_file)
        self.assertEqual(PANE_ID, args.pane_id)


if __name__ == "__main__":
    unittest.main()
