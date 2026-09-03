from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_codex_stop as codex_stop
from omo_manager import omo_source1290_prerequisite as prerequisite
from omo_manager.omo_codex_status import Report
from omo_manager.omo_report_receipt import RECEIPT_PUBLICATION_SCHEMA
from omo_manager.omo_report_receipt import RECEIPT_SCHEMA
from omo_manager.omo_report_receipt import TRANSACTION_COMMITMENT_SCHEMA
from omo_manager.omo_report_receipt import TRANSFER_RECEIPT_SCHEMA
from omo_manager.omo_report_receipt import bound_receipt_id
from omo_manager.omo_report_receipt import canonical_json
from omo_manager.omo_report_receipt import path_state
from omo_manager.tests.test_report_receipts import ReportFixture
from omo_manager.tests.test_report_receipts import cleanup_private_tmp
from omo_manager.tests.test_report_receipts import run_manager_watcher_once


SOURCE_ROOT = Path(__file__).resolve().parents[2]
SESSION_ID = "11111111-2222-3333-4444-555555555555"
PANE_ID = "%3389"
PANE_PID = 3176274
PANE_START_TICKS = 91827364
CAPTURE_SHA256 = "c" * 64
OBSERVED_SOURCE_HEAD = "c99d7d8a1b436f9f3e0d3bba20a75c8c84e8935f"
STALE_SOURCE_HEAD = "2e168e0744c976fad65308633e157cbe3942c107"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def repository_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return result.stdout.strip()


def source_record_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        raise AssertionError("source records are not a list")
    paths: list[str] = []
    for record in value:
        if not isinstance(record, dict):
            raise AssertionError("source record is not an object")
        path = record.get("path")
        if not isinstance(path, str):
            raise AssertionError("source record path is not a string")
        paths.append(path)
    return paths


def task_text(
    *,
    target: str,
    manager: bool = False,
    blocker: str = prerequisite.CANONICAL_CARRIER_BLOCKER,
    pending_items: tuple[str, ...] = (),
) -> str:
    pending_lines = ("pending_task_items: []",) if not pending_items else ("pending_task_items:", *(f"  - {item}" for item in pending_items))
    return "\n".join(
        (
            "---",
            "version: v1.0.0",
            "status: blocked",
            f"blocked_on: {blocker}",
            f"runat: {target}",
            "tool: codex",
            "managerat: vldr:0",
            f"is_manager: {str(manager).lower()}",
            *pending_lines,
            "---",
            '<human_instruction authoritative="true" source="202608/manager_mail/85c5dff58359-1290.txt:3-4">',
            "Close the “memory” thing. It is so old.",
            "Which email report was for the transcription thing",
            "</human_instruction>",
            "",
        )
    )


def running_task_text(target: str) -> str:
    return "\n".join(
        (
            "---",
            "version: v1.0.0",
            "status: running",
            f"runat: {target}",
            "tool: codex",
            "managerat: main:0",
            "is_manager: true",
            "pending_task_items: []",
            "---",
            "",
        )
    )


def finished_task_text(target: str, *, manager: bool = False) -> str:
    return "\n".join(
        (
            "---",
            "version: v1.0.0",
            "status: done",
            f"runat: {target}",
            "tool: codex",
            "managerat: vldr:0",
            f"is_manager: {str(manager).lower()}",
            "pending_task_items: []",
            "---",
            "",
        )
    )


def route_state(path: Path) -> dict[str, object]:
    payload = path.read_bytes()
    return {"exists": True, "path": str(path), "sha256": digest(payload), "size_bytes": len(payload)}


def signed(value: dict[str, object], field: str) -> dict[str, object]:
    return {**value, field: bound_receipt_id(value)}


class Fixture:
    def __init__(self, directory: Path) -> None:
        self.root = directory / "logs"
        self.root.mkdir()
        self.root.chmod(0o2775)
        state = directory / "state"
        state.mkdir(mode=0o700)
        application = state / "omo-manager"
        application.mkdir(mode=0o700)
        self.private = application / "report-receipts"
        self.private.mkdir(mode=0o700)
        self.task = self.root / prerequisite.CANONICAL_CARRIER
        self.todo = self.root / "TODO.md"
        self.duplicate = self.root / prerequisite.DUPLICATE_CARRIER
        self.manager = self.root / "manager.md"
        self.archive = self.root / "202608"
        self.archive.mkdir()
        self.archive.chmod(0o2755)
        self.archive_todo = self.root / prerequisite.ARCHIVE_TODO
        self.archived_memory = self.root / prerequisite.ARCHIVED_MEMORY
        self.archived_transcription = self.root / prerequisite.ARCHIVED_TRANSCRIPTION
        self.archived_eval = self.root / prerequisite.ARCHIVED_INTERRUPTED_EVAL
        self.archived_fix = self.root / prerequisite.ARCHIVED_INTERRUPTED_FIX
        self.audit = self.private / "completed-audit.yaml"
        self.report = self.private / "report.md"
        self.acceptance = self.private / "acceptance.json"
        self.ownership_manifest = self.private / "ownership-manifest.json"
        self.terminal_receipt = self.private / "terminal.json"
        self.task.write_text(
            task_text(target=prerequisite.TARGET, pending_items=prerequisite.CANONICAL_CARRIER_OPEN_ITEMS),
            encoding="utf-8",
        )
        self.duplicate.write_text(
            task_text(target="agent_managers:78", blocker=prerequisite.DUPLICATE_CARRIER_BLOCKER),
            encoding="utf-8",
        )
        self.manager.write_text(running_task_text("vldr:0"), encoding="utf-8")
        self.archived_memory.write_text(finished_task_text("wl:32", manager=True), encoding="utf-8")
        self.archived_transcription.write_text(finished_task_text("wl:32"), encoding="utf-8")
        self.archived_eval.write_text(finished_task_text("vldr:2"), encoding="utf-8")
        self.archived_fix.write_text(finished_task_text("vldr:1"), encoding="utf-8")
        self.archive_todo.write_text(
            "\n".join(
                (
                    "archived from todo.md previous on 2026-09-01:",
                    "memory_research_mgr.md wl:32",
                    "transcription_sw.md wl:32",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self.todo.write_text(
            "\n".join(
                (
                    "current:",
                    "manager.md vldr:0",
                    "",
                    "human pending:",
                    f"{prerequisite.CANONICAL_CARRIER} {prerequisite.TARGET}",
                    f"{prerequisite.DUPLICATE_CARRIER} agent_managers:78",
                    "",
                    "previous:",
                    "202608/mem1290_eval.md vldr:2",
                    "202608/mem1290_fix.md vldr:1",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self.audit.write_bytes(b"completed Source-1290 audit fixture\n")
        self.report.write_bytes(b"Source-1290 terminal prerequisite is ready\n")
        for path in (self.audit, self.report):
            path.chmod(0o600)
        self.refresh_ownership_manifest()
        self.report_status = prerequisite.CANONICAL_REPORT_STATUS
        self.replay_id = digest(b"source1290-report-replay")
        self.refresh_report_artifacts()
        head = repository_head(SOURCE_ROOT)
        self.args = prerequisite.Args(
            self.root.resolve(),
            digest(self.task.read_bytes()),
            digest(self.todo.read_bytes()),
            digest(self.archive_todo.read_bytes()),
            PANE_ID,
            PANE_PID,
            PANE_START_TICKS,
            SESSION_ID,
            self.report.resolve(),
            digest(self.report.read_bytes()),
            self.report_status,
            self.acceptance.resolve(),
            digest(self.acceptance.read_bytes()),
            self.audit.resolve(),
            digest(self.audit.read_bytes()),
            self.ownership_manifest.resolve(),
            digest(self.ownership_manifest.read_bytes()),
            head,
            self.terminal_receipt.resolve(),
            1.0,
            2000,
        )

    @property
    def receipt(self) -> Path:
        return self.private / f"{self.replay_id}.json"

    @property
    def publication(self) -> Path:
        return self.private / f"{self.replay_id}.publication.json"

    @property
    def commitment(self) -> Path:
        return self.private / f"{self.replay_id}.commitment"

    def refresh_ownership_manifest(self) -> None:
        self.ownership_manifest.write_bytes(prerequisite.ownership_preflight(self.root.resolve(), digest(self.todo.read_bytes())))
        self.ownership_manifest.chmod(0o600)

    def current_ownership_args(self) -> prerequisite.Args:
        self.refresh_ownership_manifest()
        return replace(
            self.args,
            todo_sha256=digest(self.todo.read_bytes()),
            ownership_manifest_sha256=digest(self.ownership_manifest.read_bytes()),
        )

    def cli_arguments(self, source_head: str) -> list[str]:
        args = self.args
        return [
            "--root",
            str(args.root),
            "--task-sha256",
            args.task_sha256,
            "--todo-sha256",
            args.todo_sha256,
            "--archive-todo-sha256",
            args.archive_todo_sha256,
            "--pane-id",
            args.pane_id,
            "--pane-pid",
            str(args.pane_pid),
            "--pane-start-ticks",
            str(args.pane_start_ticks),
            "--session-id",
            args.session_id,
            "--report-file",
            str(args.report_file),
            "--report-sha256",
            args.report_sha256,
            "--report-status",
            args.report_status,
            "--acceptance-file",
            str(args.acceptance_file),
            "--acceptance-sha256",
            args.acceptance_sha256,
            "--completed-audit",
            str(args.completed_audit),
            "--completed-audit-sha256",
            args.completed_audit_sha256,
            "--ownership-manifest",
            str(args.ownership_manifest),
            "--ownership-manifest-sha256",
            args.ownership_manifest_sha256,
            "--source-head",
            source_head,
            "--terminal-receipt",
            str(args.terminal_receipt),
            "--wait-s",
            str(args.wait_s),
            "--lines",
            str(args.lines),
        ]

    def refresh_report_artifacts(self) -> None:
        evidence = [route_state(path.resolve()) for path in sorted((self.task, self.todo, self.manager))]
        public_routing = {
            "manager": str(self.manager.resolve()),
            "producer_target": prerequisite.TARGET,
            "requested_manager_target": "vldr:0",
            "resolved_manager_target": "vldr:0",
            "route_kind": "active-manager-task",
            "task": str(self.task.resolve()),
        }
        routing = {
            "agent": "source1290-carrier",
            **public_routing,
            "root": str(self.root.resolve()),
            "route_evidence": evidence,
            "route_evidence_sha256": digest(canonical_json(evidence).rstrip(b"\n")),
            "route_local_date": "2026-09-03",
            "route_note": "",
            "tmux": {
                "pane_id": PANE_ID,
                "pane_index": "0",
                "session": "vlcontext_recovery",
                "window_index": "2",
                "window_name": "carrier",
            },
        }
        report_bytes = self.report.read_bytes()
        report_state = path_state(self.report.resolve())
        preflight_unsigned: dict[str, object] = {
            "allocation": {
                "file": str(self.report.resolve()),
                "file_path_sha256": digest(str(self.report.resolve()).encode()),
                "file_sha256": digest(report_bytes),
                "file_size_bytes": len(report_bytes),
            },
            "directories": [],
            "locks": [],
            "owner_prefix": {},
            "records": {},
            "routing_sources": evidence,
            "schema": "omo-report-preflight-transaction-set/v1",
            "temporary_files": [],
        }
        preflight = {**preflight_unsigned, "sha256": digest(canonical_json(preflight_unsigned).rstrip(b"\n"))}
        transfer_contract: dict[str, object] = {
            "authority": {
                "kind": "agent-originated",
                "producer_target": prerequisite.TARGET,
                "source_task": str(self.task.resolve()),
            },
            "commitment_path": str(self.commitment.resolve()),
            "queue_item": {
                "input_sha256": digest(report_bytes),
                "manager": str(self.manager.resolve()),
                "pointer": "fixture pointer",
                "producer": str(self.task.resolve()),
                "replay_id": self.replay_id,
            },
            "receiver": str(self.manager.resolve()),
            "routing": public_routing,
            "schema": TRANSFER_RECEIPT_SCHEMA,
        }
        commitment_unsigned: dict[str, object] = {
            "allocation": {
                "file": str(self.report.resolve()),
                "file_at_submission": report_state,
            },
            "commitment": {},
            "preflight": preflight,
            "replay_id": self.replay_id,
            "schema": TRANSACTION_COMMITMENT_SCHEMA,
            "transfer": transfer_contract,
        }
        commitment = signed(commitment_unsigned, "commitment_id")
        self.commitment.write_bytes(canonical_json(commitment))
        self.commitment.chmod(0o600)
        helper_path = SOURCE_ROOT / "omo_manager/omo_report.sh"
        receiver_path = SOURCE_ROOT / "omo_manager/omo_report_receipt.py"
        dependencies = {
            name: {"path": str(path), "sha256": digest(path.read_bytes())}
            for name, path in {
                "omo_pending_digest": SOURCE_ROOT / "omo_manager/omo_pending_digest.py",
                "omo_task_lock": SOURCE_ROOT / "omo_manager/omo_task_lock.py",
            }.items()
        }
        helper = {
            "dependencies": dependencies,
            "execution": "immutable-pipe-and-memory-compiled-sources",
            "path": str(helper_path),
            "receiver_path": str(receiver_path),
            "receiver_sha256": digest(receiver_path.read_bytes()),
            "receiver_version": "fixture",
            "receiver_version_sha256": digest(b"fixture"),
            "sha256": digest(helper_path.read_bytes()),
        }
        accepted_at = "2026-09-03T12:00:00Z"
        receipt_unsigned: dict[str, object] = {
            "accepted": True,
            "accepted_at_utc": accepted_at,
            "helper": helper,
            "input": {"sha256": digest(report_bytes), "size_bytes": len(report_bytes)},
            "preflight": preflight,
            "receipt_record": {
                "application_directory": str(self.private.parent.resolve()),
                "commit": "write-fsync-rename-fsync-directory",
                "directory": str(self.private.resolve()),
                "directory_mode": "0700",
                "file_mode": "0600",
                "final": str(self.receipt.resolve()),
                "publication_final": str(self.publication.resolve()),
                "publication_temporary": str(self.private.resolve() / f".{self.replay_id}.publication.tmp"),
                "state_home": str(self.private.parent.parent.resolve()),
                "temporary": str(self.private.resolve() / f".{self.replay_id}.tmp"),
            },
            "replay_id": self.replay_id,
            "report_context": {"attempt": None, "batch": None},
            "routing": routing,
            "schema": RECEIPT_SCHEMA,
            "side_effects": {
                "manager_acknowledgment": {"schema": "omo-pending-watch-consumed-report/v1"},
            },
            "status": self.report_status,
        }
        receipt = signed(receipt_unsigned, "receipt_id")
        self.receipt.write_bytes(canonical_json(receipt))
        self.receipt.chmod(0o600)
        publication_unsigned: dict[str, object] = {
            "receipt_id": receipt["receipt_id"],
            "receipt_path": str(self.receipt.resolve()),
            "receipt_state": path_state(self.receipt.resolve()),
            "replay_id": self.replay_id,
            "schema": RECEIPT_PUBLICATION_SCHEMA,
        }
        publication = signed(publication_unsigned, "publication_id")
        self.publication.write_bytes(canonical_json(publication))
        self.publication.chmod(0o600)
        transfer = signed({**transfer_contract, "commitment_id": commitment["commitment_id"]}, "transfer_id")
        acceptance = {
            "accepted": True,
            "accepted_at_utc": accepted_at,
            "input": {"sha256": digest(report_bytes), "size_bytes": len(report_bytes)},
            "manager_acknowledged": True,
            "publication_id": publication["publication_id"],
            "publication_path": str(self.publication.resolve()),
            "publication_state": path_state(self.publication.resolve()),
            "receipt_id": receipt["receipt_id"],
            "receipt_path": str(self.receipt.resolve()),
            "receipt_state": publication["receipt_state"],
            "reason": "manager acknowledged routed report",
            "replay_id": self.replay_id,
            "retry_required": False,
            "routing": public_routing,
            "schema": prerequisite.ACCEPTANCE_SCHEMA,
            "status": self.report_status,
            "transfer_receipt": transfer,
        }
        self.acceptance.write_bytes(canonical_json(acceptance))
        self.acceptance.chmod(0o600)

    def patches(self, command: dict[str, str], *, bypass_canonical_receipt: bool = True) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(prerequisite, "SOURCE1290_AUDIT_SHA256", digest(self.audit.read_bytes())))
        stack.enter_context(patch.object(prerequisite, "CANONICAL_SOURCE_ROOT", SOURCE_ROOT))
        stack.enter_context(
            patch.object(
                prerequisite,
                "POST_ARCHIVE_SHA256",
                {
                    prerequisite.CANONICAL_CARRIER: digest(self.task.read_bytes()),
                    prerequisite.DUPLICATE_CARRIER: digest(self.duplicate.read_bytes()),
                    prerequisite.ARCHIVED_MEMORY: digest(self.archived_memory.read_bytes()),
                    prerequisite.ARCHIVED_TRANSCRIPTION: digest(self.archived_transcription.read_bytes()),
                    prerequisite.ARCHIVED_INTERRUPTED_EVAL: digest(self.archived_eval.read_bytes()),
                    prerequisite.ARCHIVED_INTERRUPTED_FIX: digest(self.archived_fix.read_bytes()),
                },
            )
        )
        stack.enter_context(patch.object(prerequisite, "exact_pane_id", return_value=PANE_ID))
        stack.enter_context(patch.object(prerequisite, "process_start_ticks", return_value=PANE_START_TICKS))
        stack.enter_context(patch.object(prerequisite, "current_command", side_effect=lambda _target: command["value"]))
        if bypass_canonical_receipt:
            stack.enter_context(patch.object(prerequisite, "validate_canonical_receipt"))
        return stack


class Source1290PrerequisiteTests(unittest.TestCase):
    def fixture(self) -> tuple[tempfile.TemporaryDirectory[str], Fixture]:
        temporary = tempfile.TemporaryDirectory()
        return temporary, Fixture(Path(temporary.name))

    def test_blocked_human_pending_carrier_is_terminalized_without_lifecycle_edits(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        task_before = case.task.read_bytes()
        todo_before = case.todo.read_bytes()
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            command["value"] = "zsh"
            callback()
            return codex_stop.ExitedCodexShell(SESSION_ID, CAPTURE_SHA256)

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize) as exit_codex,
            patch.object(prerequisite, "validate_exited_codex_shell", return_value=CAPTURE_SHA256) as authenticate,
        ):
            output = prerequisite.reconcile(case.args)

        result = json.loads(output)
        self.assertEqual("terminalized", result["phase"])
        self.assertEqual("authenticated-exited-shell", result["terminal"]["status"])
        self.assertEqual("blocked", result["binding"]["report"]["status"])
        self.assertEqual(
            {
                "blocked_on": prerequisite.CANONICAL_CARRIER_BLOCKER,
                "pending_task_items": {
                    "items": list(prerequisite.CANONICAL_CARRIER_OPEN_ITEMS),
                    "sha256": prerequisite.CANONICAL_CARRIER_OPEN_ITEMS_SHA256,
                },
                "status": "blocked",
                "todo_section": "human pending",
                "transition": "none",
            },
            result["binding"]["carrier_lifecycle"],
        )
        self.assertEqual(output, case.terminal_receipt.read_bytes())
        self.assertEqual(task_before, case.task.read_bytes())
        self.assertEqual(todo_before, case.todo.read_bytes())
        exit_codex.assert_called_once()
        authenticate.assert_called_once()

    def test_bounded_manifest_ignores_780_shared_mode_historical_records_and_allocates_receipt(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        history = case.root / "202604"
        history.mkdir()
        for index in range(780):
            record = history / f"{index:03d}_historical.md"
            record.write_text("preserved historical record\n", encoding="utf-8")
            record.chmod(0o2664)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            command["value"] = "zsh"
            return codex_stop.ExitedCodexShell(SESSION_ID, CAPTURE_SHA256)

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            patch.object(prerequisite, "validate_exited_codex_shell", return_value=CAPTURE_SHA256),
        ):
            output = prerequisite.reconcile(case.args)

        terminalized = json.loads(output)
        indexed = terminalized["binding"]["ownership_manifest"]["index"]["tasks"]
        self.assertEqual(5, len(indexed))
        self.assertEqual("terminalized", terminalized["phase"])
        self.assertTrue(case.terminal_receipt.exists())

    def test_ownership_preflight_cli_emits_the_canonical_bounded_index(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SOURCE_ROOT / "omo_manager/omo_source1290_prerequisite.py"),
                "ownership-preflight",
                "--root",
                str(case.root),
                "--todo-sha256",
                digest(case.todo.read_bytes()),
            ],
            cwd=SOURCE_ROOT,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        manifest = json.loads(result.stdout)
        self.assertEqual(result.stdout, canonical_json(manifest))
        self.assertEqual(
            ["202608/mem1290_eval.md", "202608/mem1290_fix.md", "manager.md", "mem1290_auth.md", "memory_auth_1290.md"],
            [item["path"] for item in manifest["tasks"]],
        )

    def test_manifest_cannot_exclude_an_indexed_active_owner(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        second = case.root / "second-owner.md"
        second.write_text(task_text(target=prerequisite.TARGET), encoding="utf-8")
        case.todo.write_text(
            case.todo.read_text(encoding="utf-8").replace("manager.md vldr:0\n", "manager.md vldr:0\nsecond-owner.md vlcontext_recovery:2\n"),
            encoding="utf-8",
        )
        args = case.current_ownership_args()
        manifest = json.loads(case.ownership_manifest.read_bytes())
        manifest["tasks"] = [item for item in manifest["tasks"] if item["path"] != "second-owner.md"]
        case.ownership_manifest.write_bytes(canonical_json(manifest))
        args = replace(args, ownership_manifest_sha256=digest(case.ownership_manifest.read_bytes()))
        command = {"value": "bunx"}
        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "manifest drifted from the authoritative TODO task index"),
        ):
            prerequisite.reconcile(args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_accepted_false_refuses_before_intent_or_terminal_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        acceptance = json.loads(case.acceptance.read_bytes())
        acceptance["accepted"] = False
        acceptance["manager_acknowledged"] = False
        case.acceptance.write_bytes(canonical_json(acceptance))
        args = replace(case.args, acceptance_sha256=digest(case.acceptance.read_bytes()))
        with patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize, self.assertRaisesRegex(prerequisite.PrerequisiteError, "accepted:true"):
            prerequisite.reconcile(args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_missing_durable_report_receipt_refuses_terminalization(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        case.receipt.unlink()
        with patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize, self.assertRaisesRegex(prerequisite.PrerequisiteError, "cannot open durable report receipt"):
            prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_added_or_removed_indexed_task_invalidates_manifest(self) -> None:
        for change in ("added", "removed"):
            with self.subTest(change=change):
                temporary, case = self.fixture()
                self.addCleanup(temporary.cleanup)
                if change == "added":
                    (case.root / "added.md").write_text(finished_task_text("spare:1"), encoding="utf-8")
                    todo_text = case.todo.read_text(encoding="utf-8").replace("manager.md vldr:0\n", "manager.md vldr:0\nadded.md spare:1\n")
                else:
                    todo_text = case.todo.read_text(encoding="utf-8").replace("manager.md vldr:0\n", "")
                case.todo.write_text(todo_text, encoding="utf-8")
                args = replace(case.args, todo_sha256=digest(case.todo.read_bytes()))
                command = {"value": "bunx"}
                with (
                    case.patches(command),
                    patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
                    self.assertRaisesRegex(prerequisite.PrerequisiteError, "manifest drifted from the authoritative TODO task index"),
                ):
                    prerequisite.reconcile(args)
                terminalize.assert_not_called()
                self.assertFalse(case.terminal_receipt.exists())

    def test_duplicate_target_alias_is_an_unexpected_active_owner(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        second = case.root / "second-owner.md"
        second.write_text(task_text(target=f"{prerequisite.TARGET}.0"), encoding="utf-8")
        case.todo.write_text(
            case.todo.read_text(encoding="utf-8").replace("manager.md vldr:0\n", "manager.md vldr:0\nsecond-owner.md vlcontext_recovery:2.0\n"),
            encoding="utf-8",
        )
        args = case.current_ownership_args()
        command = {"value": "bunx"}
        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "canonical Source-1290 carrier is not the sole target owner"),
        ):
            prerequisite.reconcile(args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_manifest_digest_or_membership_duplication_is_refused(self) -> None:
        for change in ("digest", "duplicate"):
            with self.subTest(change=change):
                temporary, case = self.fixture()
                self.addCleanup(temporary.cleanup)
                if change == "digest":
                    case.ownership_manifest.write_bytes(case.ownership_manifest.read_bytes() + b" ")
                    args = case.args
                    message = "ownership manifest bytes do not match the supplied digest"
                else:
                    manifest = json.loads(case.ownership_manifest.read_bytes())
                    manifest["tasks"].append(dict(manifest["tasks"][0]))
                    case.ownership_manifest.write_bytes(canonical_json(manifest))
                    args = replace(case.args, ownership_manifest_sha256=digest(case.ownership_manifest.read_bytes()))
                    message = "ownership manifest task set is omitted, duplicated, or unordered"
                with (
                    patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
                    self.assertRaisesRegex(prerequisite.PrerequisiteError, message),
                ):
                    prerequisite.reconcile(args)
                terminalize.assert_not_called()
                self.assertFalse(case.terminal_receipt.exists())

    def test_indexed_task_symlink_type_or_mode_drift_is_refused(self) -> None:
        for change in ("symlink", "type", "mode"):
            with self.subTest(change=change):
                temporary, case = self.fixture()
                self.addCleanup(temporary.cleanup)
                if change == "symlink":
                    case.manager.unlink()
                    case.manager.symlink_to(case.duplicate)
                elif change == "type":
                    case.manager.unlink()
                    case.manager.mkdir()
                else:
                    case.manager.chmod(0o664)
                command = {"value": "bunx"}
                with (
                    case.patches(command),
                    patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
                    self.assertRaises((prerequisite.PrerequisiteError, OSError)),
                ):
                    prerequisite.reconcile(case.args)
                terminalize.assert_not_called()
                self.assertFalse(case.terminal_receipt.exists())

    def test_indexed_task_parent_mode_drift_is_refused(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(0o2775, case.root.stat().st_mode & 0o7777)
        case.root.chmod(0o2755)
        command = {"value": "bunx"}
        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "manifest drifted from the authoritative TODO task index"),
        ):
            prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_underspecified_receipt_side_effects_are_not_accepted(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}
        with (
            case.patches(command, bypass_canonical_receipt=False),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "canonical side-effect schema"),
        ):
            prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_real_canonical_receipt_passes_and_acknowledgment_tamper_fails(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        fixture_root = Path(temporary.name)
        home = fixture_root / "home"
        home.mkdir()
        bin_directory = fixture_root / "bin"
        bin_directory.mkdir()
        tmux = bin_directory / "tmux"
        tmux.write_text(
            "#!/usr/bin/env bash\nprintf 'vlcontext_recovery\\t2\\t0\\t%%3389\\tcarrier\\n'\n",
            encoding="utf-8",
        )
        tmux.chmod(0o700)
        local_env = fixture_root / "local.env"
        local_env.write_text(
            f"OMO_WORK_LOGS_ROOT={case.root}\nOMO_MANAGER_TMUX_TARGET=main:0\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        for name in (
            "OMO_WORK_LOGS_ROOT",
            "OMO_MANAGER_TMUX_TARGET",
            "OMO_AGENT_NAME",
            "OMO_REPORT_DESCRIPTION_ROUTE_ATTEMPT",
            "OMO_REPORT_DESCRIPTION_ROUTE_RETRY_FD",
            "TMUX",
        ):
            environment.pop(name, None)
        environment.update(
            {
                "HOME": str(home),
                "XDG_STATE_HOME": str(case.private.parent.parent),
                "OMO_MANAGER_LOCAL_ENV": str(local_env),
                "OMO_REPORT_ACK_TIMEOUT_S": "0",
                "PATH": f"{bin_directory}:{environment['PATH']}",
                "TMUX_PANE": PANE_ID,
            }
        )
        command = [
            str(SOURCE_ROOT / "omo_manager/omo_report.sh"),
            "--status",
            case.report_status,
            "--message-file",
            str(case.report),
            "--agent",
            "source1290-carrier",
        ]
        pending = subprocess.run(
            command,
            cwd=fixture_root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, pending.returncode, pending.stderr)
        self.assertIs(json.loads(pending.stdout)["accepted"], False)
        report_case = ReportFixture(case.root, case.report, environment)
        watched = run_manager_watcher_once(report_case, case.manager)
        self.assertEqual(0, watched.returncode, watched.stderr)
        accepted = subprocess.run(
            command,
            cwd=fixture_root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        acceptance = json.loads(accepted.stdout)
        self.assertIs(acceptance["accepted"], True)
        receipt_path = Path(str(acceptance["receipt_path"]))
        receipt = json.loads(receipt_path.read_bytes())
        self.addCleanup(cleanup_private_tmp, receipt)
        case.acceptance.write_bytes(canonical_json(acceptance))
        case.acceptance.chmod(0o600)
        args = replace(case.args, acceptance_sha256=digest(case.acceptance.read_bytes()))
        paths = prerequisite.report_paths(args)
        report_snapshot = prerequisite.stable_owned_read(
            case.report,
            label="canonical report integration fixture",
            exact_mode=0o600,
            private_parent=True,
        )
        receipt, receipt_snapshot = prerequisite.json_snapshot(receipt_path, label="canonical receipt integration fixture")
        prerequisite.validate_canonical_receipt(args, paths, report_snapshot, receipt, receipt_snapshot)

        effects = receipt["side_effects"]
        assert isinstance(effects, dict)
        acknowledgment = effects["manager_acknowledgment"]
        assert isinstance(acknowledgment, dict)
        transition = acknowledgment["transition"]
        assert isinstance(transition, dict)
        transition["protocol"] = "forged-transition"
        unsigned = dict(receipt)
        unsigned.pop("receipt_id")
        receipt["receipt_id"] = bound_receipt_id(unsigned)
        receipt_path.write_bytes(canonical_json(receipt))
        receipt_path.chmod(0o600)
        tampered, tampered_snapshot = prerequisite.json_snapshot(receipt_path, label="tampered receipt integration fixture")
        with self.assertRaisesRegex(prerequisite.PrerequisiteError, "manager transition is inconsistent"):
            prerequisite.validate_canonical_receipt(args, paths, report_snapshot, tampered, tampered_snapshot)

    def test_carrier_requires_one_human_pending_row(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        original = case.todo.read_text(encoding="utf-8")
        for change in ("wrong section", "duplicate row", "missing row"):
            with self.subTest(change=change):
                if change == "wrong section":
                    changed = original.replace("current:\n", f"current:\n{prerequisite.CANONICAL_CARRIER} {prerequisite.TARGET}\n", 1)
                    changed = changed.replace(f"human pending:\n{prerequisite.CANONICAL_CARRIER} {prerequisite.TARGET}\n", "human pending:\n", 1)
                elif change == "duplicate row":
                    changed = original.replace(
                        "current:\n",
                        f"current:\n{prerequisite.CANONICAL_CARRIER} {prerequisite.TARGET}\n",
                        1,
                    )
                else:
                    changed = original.replace(f"{prerequisite.CANONICAL_CARRIER} {prerequisite.TARGET}\n", "", 1)
                case.todo.write_text(changed, encoding="utf-8")
                args = replace(case.args, todo_sha256=digest(case.todo.read_bytes()))
                command = {"value": "bunx"}
                with (
                    case.patches(command),
                    patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
                    self.assertRaisesRegex(prerequisite.PrerequisiteError, "TODO placement"),
                ):
                    prerequisite.reconcile(args)
                terminalize.assert_not_called()
                self.assertFalse(case.terminal_receipt.exists())
                case.todo.write_text(original, encoding="utf-8")

    def test_carrier_open_items_must_match_exact_order_and_membership(self) -> None:
        original = prerequisite.CANONICAL_CARRIER_OPEN_ITEMS
        variants = {
            "reordered": tuple(reversed(original)),
            "added": (*original, "Unexpected third item."),
            "removed": original[:1],
        }
        for change, pending_items in variants.items():
            with self.subTest(change=change):
                temporary, case = self.fixture()
                self.addCleanup(temporary.cleanup)
                case.task.write_text(task_text(target=prerequisite.TARGET, pending_items=pending_items), encoding="utf-8")
                args = replace(case.args, task_sha256=digest(case.task.read_bytes()))
                command = {"value": "bunx"}
                with (
                    case.patches(command),
                    patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
                    self.assertRaisesRegex(prerequisite.PrerequisiteError, "exact blocked/open-item"),
                ):
                    prerequisite.reconcile(args)
                terminalize.assert_not_called()
                self.assertFalse(case.terminal_receipt.exists())

    def test_concurrent_open_item_reorder_after_intent_is_detected_before_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            case.task.write_text(
                task_text(target=prerequisite.TARGET, pending_items=tuple(reversed(prerequisite.CANONICAL_CARRIER_OPEN_ITEMS))),
                encoding="utf-8",
            )
            callback = args[6]
            assert callable(callback)
            callback()
            raise AssertionError("unreachable")

        with case.patches(command), patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize), self.assertRaisesRegex(prerequisite.PrerequisiteError, "drifted"):
            prerequisite.reconcile(case.args)
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])
        self.assertEqual("bunx", command["value"])

    def test_report_receipt_change_after_intent_is_detected_before_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            case.receipt.write_bytes(case.receipt.read_bytes() + b" ")
            callback = args[6]
            assert callable(callback)
            callback()
            raise AssertionError("unreachable")

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "lifecycle evidence drifted"),
        ):
            prerequisite.reconcile(case.args)
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])

    def test_canonical_source_head_command_ignores_repository_environment_overrides(self) -> None:
        temporary, _case = self.fixture()
        self.addCleanup(temporary.cleanup)
        self.assertEqual(Path("/home/sichangheagent/.config"), prerequisite.CANONICAL_SOURCE_ROOT)
        observation = subprocess.CompletedProcess([], 0, f"{OBSERVED_SOURCE_HEAD}\n", "")
        overrides = {
            "GIT_COMMON_DIR": str(SOURCE_ROOT / ".git"),
            "GIT_DIR": str(SOURCE_ROOT / ".git"),
            "GIT_WORK_TREE": str(SOURCE_ROOT),
            "HOME": str(Path(temporary.name) / "untrusted-home"),
        }
        with patch.dict(os.environ, overrides), patch.object(subprocess, "run", return_value=observation) as run:
            self.assertEqual(OBSERVED_SOURCE_HEAD, prerequisite.git_head())
        run.assert_called_once()
        positional, keywords = run.call_args
        self.assertEqual(
            (["git", "-C", "/home/sichangheagent/.config", "rev-parse", "HEAD"],),
            positional,
        )
        self.assertEqual("untrusted-home", Path(keywords["env"]["HOME"]).name)
        self.assertFalse(any(name.startswith("GIT_") for name in keywords["env"]))
        self.assertEqual(
            {"capture_output": True, "check": False, "text": True, "timeout": 10},
            {name: value for name, value in keywords.items() if name != "env"},
        )

    def test_source_head_claim_accepts_current_and_rejects_stale_different_or_non_full_sha(self) -> None:
        with patch.object(prerequisite, "git_head", return_value=OBSERVED_SOURCE_HEAD) as observe:
            prerequisite.require_source_head(OBSERVED_SOURCE_HEAD)
            for source_head in (STALE_SOURCE_HEAD, "f" * 40):
                with self.subTest(source_head=source_head), self.assertRaisesRegex(prerequisite.PrerequisiteError, "differs from --source-head"):
                    prerequisite.require_source_head(source_head)
        self.assertEqual(3, observe.call_count)

        for source_head in (OBSERVED_SOURCE_HEAD[:12], OBSERVED_SOURCE_HEAD.upper(), "not-a-sha"):
            with (
                self.subTest(source_head=source_head),
                patch.object(prerequisite, "git_head") as observe,
                self.assertRaisesRegex(prerequisite.PrerequisiteError, "full lowercase Git SHA"),
            ):
                prerequisite.require_source_head(source_head)
            observe.assert_not_called()

    def test_source_cli_manifest_has_only_fresh_head_evidence(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        with (
            patch.object(prerequisite, "SOURCE1290_AUDIT_SHA256", case.args.completed_audit_sha256),
            patch.object(prerequisite, "git_head", return_value=OBSERVED_SOURCE_HEAD),
        ):
            parsed = prerequisite.parse_args(case.cli_arguments(OBSERVED_SOURCE_HEAD))
            self.assertEqual(OBSERVED_SOURCE_HEAD, parsed.source_head)
            for source_head in (STALE_SOURCE_HEAD, "f" * 40, OBSERVED_SOURCE_HEAD[:12], OBSERVED_SOURCE_HEAD.upper(), "not-a-sha"):
                with self.subTest(source_head=source_head), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    prerequisite.parse_args(case.cli_arguments(source_head))
            for option in ("--source-root", "--source-ref", "--work-tree"):
                with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    prerequisite.parse_args([*case.cli_arguments(OBSERVED_SOURCE_HEAD), option, str(SOURCE_ROOT)])

    def test_source_binding_authenticates_every_installed_source_file(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}
        with case.patches(command), patch.object(prerequisite, "git_head", return_value=case.args.source_head) as observe:
            binding = prerequisite.source_binding(case.args)
        self.assertEqual(case.args.source_head, binding["head"])
        self.assertEqual(
            [str((SOURCE_ROOT / relative).resolve()) for relative in prerequisite.SOURCE_FILES],
            source_record_paths(binding["files"]),
        )
        self.assertEqual(2, observe.call_count)

    def test_concurrent_source_head_drift_during_snapshot_refuses_before_intent_or_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}
        with (
            case.patches(command),
            patch.object(prerequisite, "git_head", side_effect=(case.args.source_head, "f" * 40)),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "differs from --source-head"),
        ):
            prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_source_head_change_after_intent_is_detected_before_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            raise AssertionError("unreachable")

        with (
            case.patches(command),
            patch.object(prerequisite, "git_head", side_effect=(case.args.source_head, case.args.source_head, "f" * 40)),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "lifecycle evidence drifted"),
        ):
            prerequisite.reconcile(case.args)
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])

    def test_unrelated_installed_source_byte_drift_is_detected_before_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        source_root = Path(temporary.name) / "installed-source"
        for relative in prerequisite.SOURCE_FILES:
            destination = source_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((SOURCE_ROOT / relative).read_bytes())
        unrelated = source_root / prerequisite.SOURCE_FILES[-1]
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            unrelated.write_bytes(unrelated.read_bytes() + b"\n")
            callback = args[6]
            assert callable(callback)
            callback()
            raise AssertionError("unreachable")

        with (
            case.patches(command),
            patch.object(prerequisite, "CANONICAL_SOURCE_ROOT", source_root),
            patch.object(prerequisite, "__file__", str(source_root / prerequisite.SOURCE_FILES[0])),
            patch.object(prerequisite, "git_head", return_value=case.args.source_head),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "lifecycle evidence drifted"),
        ):
            prerequisite.reconcile(case.args)
        prepared = json.loads(case.terminal_receipt.read_bytes())
        self.assertEqual("prepared", prepared["phase"])
        self.assertEqual(
            [str((source_root / relative).resolve()) for relative in prerequisite.SOURCE_FILES],
            source_record_paths(prepared["binding"]["source"]["files"]),
        )
        self.assertEqual("bunx", command["value"])

    def test_concurrent_second_owner_after_intent_is_detected_before_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            (case.root / "second-owner.md").write_text(task_text(target=prerequisite.TARGET), encoding="utf-8")
            case.todo.write_text(
                case.todo.read_text(encoding="utf-8").replace("manager.md vldr:0\n", "manager.md vldr:0\nsecond-owner.md vlcontext_recovery:2.0\n"),
                encoding="utf-8",
            )
            callback = args[6]
            assert callable(callback)
            callback()
            raise AssertionError("unreachable")

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "lifecycle evidence drifted"),
        ):
            prerequisite.reconcile(case.args)
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])

    def test_archived_transcription_drift_after_intent_is_detected_before_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            case.archived_transcription.write_bytes(case.archived_transcription.read_bytes() + b"drift\n")
            callback = args[6]
            assert callable(callback)
            callback()
            raise AssertionError("unreachable")

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "lifecycle evidence drifted"),
        ):
            prerequisite.reconcile(case.args)
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])

    def test_pane_identity_mismatch_refuses_before_intent_or_input(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}
        with (
            case.patches(command),
            patch.object(prerequisite, "exact_pane_id", return_value="%9999"),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "pane identity drifted"),
        ):
            prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_bare_exited_shell_without_prepared_intent_is_not_adopted(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "zsh"}
        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            patch.object(prerequisite, "validate_exited_codex_shell") as authenticate,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "missing its durable pre-terminalization intent"),
        ):
            prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        authenticate.assert_not_called()
        self.assertFalse(case.terminal_receipt.exists())

    def test_interruption_after_shell_exit_recovers_once_and_replays_identically(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def interrupted(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            command["value"] = "zsh"
            raise KeyboardInterrupt

        with case.patches(command), patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            prerequisite.reconcile(case.args)
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])
        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell") as terminalize,
            patch.object(prerequisite, "validate_exited_codex_shell", return_value=CAPTURE_SHA256),
        ):
            terminalized = prerequisite.reconcile(case.args)
            replay = prerequisite.reconcile(case.args)
        terminalize.assert_not_called()
        self.assertEqual(terminalized, replay)
        self.assertEqual("terminalized", json.loads(terminalized)["phase"])

    def test_interruption_after_first_exit_input_while_codex_is_live_resumes_prepared_intent(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def interrupted(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            raise KeyboardInterrupt

        with case.patches(command), patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            prerequisite.reconcile(case.args)
        self.assertEqual("bunx", command["value"])
        self.assertEqual("prepared", json.loads(case.terminal_receipt.read_bytes())["phase"])

        def resumed(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            command["value"] = "zsh"
            return codex_stop.ExitedCodexShell(SESSION_ID, CAPTURE_SHA256)

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=resumed) as terminalize,
            patch.object(prerequisite, "validate_exited_codex_shell", return_value=CAPTURE_SHA256),
        ):
            terminalized = prerequisite.reconcile(case.args)
            replay = prerequisite.reconcile(case.args)
        terminalize.assert_called_once()
        self.assertEqual(terminalized, replay)
        self.assertEqual("terminalized", json.loads(terminalized)["phase"])

    def test_terminalized_replay_rejects_sibling_transaction_temporary(self) -> None:
        temporary, case = self.fixture()
        self.addCleanup(temporary.cleanup)
        command = {"value": "bunx"}

        def terminalize(*args: object, **_kwargs: object) -> codex_stop.ExitedCodexShell:
            callback = args[6]
            assert callable(callback)
            callback()
            command["value"] = "zsh"
            return codex_stop.ExitedCodexShell(SESSION_ID, CAPTURE_SHA256)

        with (
            case.patches(command),
            patch.object(prerequisite, "terminalize_bound_codex_to_shell", side_effect=terminalize),
            patch.object(prerequisite, "validate_exited_codex_shell", return_value=CAPTURE_SHA256),
        ):
            prerequisite.reconcile(case.args)
        residue = case.terminal_receipt.with_name(f".{case.terminal_receipt.name}.tmp")
        residue.write_bytes(b"{}\n")
        residue.chmod(0o600)
        with (
            case.patches(command),
            patch.object(prerequisite, "validate_exited_codex_shell") as authenticate,
            self.assertRaisesRegex(prerequisite.PrerequisiteError, "unexpected transaction residue"),
        ):
            prerequisite.reconcile(case.args)
        authenticate.assert_not_called()


class BoundTerminalizationTests(unittest.TestCase):
    def common_patches(self, capture: str) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch.object(codex_stop, "target_session_name", return_value="cfg"))
        stack.enter_context(patch.object(codex_stop, "pane_id", return_value="%42"))
        stack.enter_context(patch.object(codex_stop, "current_pane_id", return_value="%caller"))
        stack.enter_context(patch.object(codex_stop, "process_start_ticks", return_value=73))
        stack.enter_context(
            patch.object(
                codex_stop,
                "bound_guarded_read",
                side_effect=lambda *_args: "%42\tcfg:1.0\n" if "#{pane_id}" in _args[2][-1] else "cfg:1.0\n",
            )
        )
        stack.enter_context(patch.object(codex_stop, "guarded_capture", return_value=capture))
        stack.enter_context(patch.object(codex_stop, "report_from_lines", return_value=Report("ready", [])))
        return stack

    def test_bound_terminalization_keeps_authenticated_shell_pane(self) -> None:
        token = "accepted-receipt-token"
        wrapped_token = f"{token[:10]}\n{token[10:]}"
        capture = f'{{"accepted":true,"receipt_id":"{wrapped_token}"}}\n› ready\n'
        checks: list[str] = []
        with (
            self.common_patches(capture),
            patch.object(codex_stop, "query_status_session_id", return_value=(SESSION_ID, "status")) as status,
            patch.object(codex_stop, "send_exit_keys") as interrupt,
            patch.object(codex_stop, "wait_shell", return_value=True),
            patch.object(codex_stop, "validate_exited_codex_shell", return_value=CAPTURE_SHA256),
            patch.object(codex_stop, "close_tmux_target") as close,
        ):
            result = codex_stop.terminalize_bound_codex_to_shell(
                "cfg:1",
                "%42",
                4242,
                73,
                SESSION_ID,
                token,
                lambda: checks.append("checked"),
            )
        self.assertEqual(codex_stop.ExitedCodexShell(SESSION_ID, CAPTURE_SHA256), result)
        status.assert_called_once()
        interrupt.assert_called_once()
        close.assert_not_called()
        self.assertGreaterEqual(len(checks), 2)

    def test_bound_terminalization_requires_visible_accepted_evidence_before_input(self) -> None:
        with (
            self.common_patches("› ready\n"),
            patch.object(codex_stop, "query_status_session_id") as status,
            patch.object(codex_stop, "send_exit_keys") as interrupt,
            self.assertRaisesRegex(RuntimeError, "accepted terminal report evidence is absent"),
        ):
            codex_stop.terminalize_bound_codex_to_shell("cfg:1", "%42", 4242, 73, SESSION_ID, "accepted-receipt-token", lambda: None)
        status.assert_not_called()
        interrupt.assert_not_called()

    def test_bound_terminalization_resumes_after_one_in_progress_interrupt_marker(self) -> None:
        token = "accepted-receipt-token"
        capture = f'{{"accepted":true,"receipt_id":"{token}"}}\nConversation interrupted\n› ready\n'
        with (
            self.common_patches(capture),
            patch.object(codex_stop, "query_status_session_id", return_value=(SESSION_ID, "status")) as status,
            patch.object(codex_stop, "send_exit_keys") as interrupt,
            patch.object(codex_stop, "wait_shell", return_value=True),
            patch.object(codex_stop, "validate_exited_codex_shell", return_value=CAPTURE_SHA256),
        ):
            result = codex_stop.terminalize_bound_codex_to_shell(
                "cfg:1",
                "%42",
                4242,
                73,
                SESSION_ID,
                token,
                lambda: None,
            )
        self.assertEqual(codex_stop.ExitedCodexShell(SESSION_ID, CAPTURE_SHA256), result)
        status.assert_called_once()
        interrupt.assert_called_once()

    def test_bound_terminalization_refuses_completed_prior_exit_marker(self) -> None:
        token = "accepted-receipt-token"
        capture = f'{{"accepted":true,"receipt_id":"{token}"}}\nConversation interrupted\nTo continue this session, run codex resume {SESSION_ID}\n$ '
        with (
            self.common_patches(capture),
            patch.object(codex_stop, "query_status_session_id") as status,
            patch.object(codex_stop, "send_exit_keys") as interrupt,
            self.assertRaisesRegex(RuntimeError, "completed prior exit marker"),
        ):
            codex_stop.terminalize_bound_codex_to_shell("cfg:1", "%42", 4242, 73, SESSION_ID, token, lambda: None)
        status.assert_not_called()
        interrupt.assert_not_called()

    def test_bound_terminalization_rechecks_exact_pane_before_interrupt(self) -> None:
        token = "accepted-receipt-token"
        capture = f'{{"accepted":true,"receipt_id":"{token}"}}\n› ready\n'
        reads = iter(("cfg:1.0\n", "%41\tcfg:1.0\n"))
        with (
            self.common_patches(capture),
            patch.object(codex_stop, "bound_guarded_read", side_effect=lambda *_args: next(reads)),
            patch.object(codex_stop, "query_status_session_id", return_value=(SESSION_ID, "status")),
            patch.object(codex_stop, "send_exit_keys") as interrupt,
            self.assertRaisesRegex(RuntimeError, "identity changed before interrupt"),
        ):
            codex_stop.terminalize_bound_codex_to_shell("cfg:1", "%42", 4242, 73, SESSION_ID, token, lambda: None)
        interrupt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
