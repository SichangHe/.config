from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import final
from unittest.mock import patch

import omo_manager.omo_namespace_drain as namespace_drain

from omo_manager.omo_namespace_drain import (
    CODE_REVIEW_SCHEMA,
    COMPLETED_SHELL_BLOCKER,
    COMPLETED_SHELL_MESSAGE_ID,
    COMPLETED_SHELL_PANE,
    COMPLETED_SHELL_TASK,
    DOC_PATH,
    TASKLESS_SHELL_TARGET,
    TEST_PATH,
    UNSLOP_ACCEPTED_EVIDENCE,
    UNSLOP_AUTHORITY_TEXT,
    UNSLOP_BLOCKER,
    UNSLOP_PANE,
    UNSLOP_TASK,
    UNSLOP_TARGET,
    ABSENT_MANAGER_PUBLIC_MUTATION_BLOCKER,
    SOURCE1385_AUTHORITY_TEXT,
    SOURCE1385_PUBLIC_MUTATION_BLOCKER,
    DrainError,
    PreparedShellRecovery,
    SharedPaneSnapshot,
    ShellProcessSnapshot,
    ShellSnapshot,
    atomic_replace_task,
    bind_absent_manager_history,
    bind_pinned_audit_object,
    bind_recovery_phase_objects,
    bind_completed_shell,
    bind_taskless_shell,
    bind_unslop_done,
    bind_source1385_live_worker_handoff,
    build_plan,
    canonical_json,
    capture_active_graph,
    close_completed_shell,
    close_absent_manager_history,
    close_taskless_shell,
    close_unslop_done,
    close_source1385_live_worker_handoff,
    completed_task_replacements,
    execute,
    enumerate_task_record_paths,
    guarded_remove_shell,
    inspect_target,
    main,
    managed_agent_descendant,
    lifecycle_exchange_slot_name,
    pinned_directory,
    prestage_lifecycle_exchange_slot,
    prepare_shell_recovery,
    publish_or_validate_staged_audit,
    repository_dirty_snapshot,
    record_source1385_phase,
    raw_commit_contains_ancestor,
    recoverable_replace_task,
    recovery_phase_from_entries,
    recovery_phase_staged_name,
    recovery_paths,
    sha256_bytes,
    shell_snapshot,
    shell_removal_attempt_state,
    shared_pane_snapshot,
    stop_target,
    source1385_task_replacements,
    source1385_todo_replacement,
    validate_source1385_successor_eligibility,
    unslop_task_replacement,
    unslop_graph_cas_value,
    validate_unslop_authority,
    transition_shell_recovery,
    validate_code_review,
    validate_new_private_output,
    validate_pinned_object_binding,
    verify_consumed_report,
)
from omo_manager.omo_completion_email import build_completion_email
from omo_manager.omo_report_receipt import bound_receipt_id
from omo_manager.omo_task_metadata import parse_task_metadata


def shell_fixture(pane_id: str = "%7", pane_pid: int = 101, pane_ticks: int = 202) -> ShellSnapshot:
    child_pid = pane_pid + 1
    root = ShellProcessSnapshot(
        pane_pid,
        pane_ticks,
        "S",
        "zsh",
        "1" * 64,
        "/usr/bin/zsh",
        1,
        2,
        0o755,
        805677,
        pane_pid,
        pane_pid,
        child_pid,
        34832,
    )
    foreground = ShellProcessSnapshot(
        child_pid,
        pane_ticks + 1,
        "S",
        "fish",
        "2" * 64,
        "/nix/store/fish/bin/fish",
        1,
        3,
        0o555,
        pane_pid,
        pane_pid,
        child_pid,
        child_pid,
        34832,
    )
    return ShellSnapshot(
        pane_id,
        pane_pid,
        pane_ticks,
        "fish",
        sha256_bytes(b"\n"),
        "3" * 64,
        root,
        foreground,
        "/ssd1/sichangheagent/opsmail0802",
        "uscnsl-exxact-server",
        1,
        202,
        62,
        2,
        0,
    )


def task(target: str, pending: list[str] | None = None, status: str = "blocked") -> str:
    blocker = "blocked_on: preserved blocker\n" if status == "blocked" else ""
    items = "\n".join(f"  - {item}" for item in pending or [])
    return f"""---
version: v1.0.0
status: {status}
{blocker}runat: {target}
tool: codex
managerat: root:0
is_manager: false
pending_task_items:
{items}
---
body
"""


def v2_task(target: str, status: str = "blocked") -> str:
    state = "status: blocked\nresume_status: running\nblocked_on:\n  - kind: persistent\n    reason: original v2 blocker" if status == "blocked" else f"status: {status}"
    return f"""---
version: v2.0.0
task_id: task_00000000-0000-7000-8000-000000000001
{state}
runat: {target}
tool: codex
managerat: root:0
is_manager: false
pending_task_items: []
resolved_task_items:
  - id: pi_00000000-0000-7000-8000-000000000002
    outcome: completed
    evidence: preserved evidence
    resolved_at: 2026-08-30T00:00:00Z
    notices: []
---
v2 body
"""


def completed_task() -> str:
    return f"""---
version: v1.0.0
status: blocked
blocked_on: {COMPLETED_SHELL_BLOCKER}
runat: agent_managers:39
tool: codex
managerat: owner:1
is_manager: false
pending_task_items: []
---
Existing owner-authenticated completion notice was accepted as Message-ID {COMPLETED_SHELL_MESSAGE_ID}.
"""


def manager_task() -> str:
    return """---
version: v1.0.0
status: running
runat: owner:1
tool: codex
managerat: upper:1
is_manager: true
pending_task_items: []
---
owner
"""


def todo_for_completed_task() -> str:
    return """current:
amh1232_term_eval.md agent_managers:39

previous:
"""


def unslop_task() -> str:
    return f"""---
version: v1.0.0
status: blocked
blocked_on: {UNSLOP_BLOCKER}
runat: {UNSLOP_TARGET}
tool: codex
managerat: wl:3
is_manager: false
pending_task_items: []
---
{UNSLOP_ACCEPTED_EVIDENCE}
"""


def active_manager_task(target: str, manager: str = "upper:1") -> str:
    return f"""---
version: v1.0.0
status: running
runat: {target}
tool: codex
managerat: {manager}
is_manager: true
pending_task_items: []
---
manager
"""


def absent_manager_task(target: str = "opsmail0802:1", pending: list[str] | None = None) -> str:
    items = pending or [
        "Resolve only exact untracked empty shell opsmail0802:0.0 through a supported taskless exact-target disposition.",
        "Preserve ordered custody evidence.",
    ]
    queue = "\n".join(f"  - {item}" for item in items)
    return f"""---
version: v1.0.0
status: long_running
blocked_on: Source-1261 authenticated namespace drain must provide supported exact taskless-shell disposition
runat: {target}
tool: codex
managerat: wl:3
is_manager: true
pending_task_items:
{queue}
session_id: 01a03689-cc8c-7673-947f-9d2c0b33f69a
---
body and history stay opaque
"""


def absent_manager_todo(target: str = "opsmail0802:1") -> str:
    return f"""current:
mail_archive_ops_submgr_0802.md {target}

previous:
"""


def shared_snapshot(*, status: str = "ready", pane_pid: int = 806864, agent_pid: int = 4121489) -> SharedPaneSnapshot:
    return SharedPaneSnapshot(
        UNSLOP_PANE,
        pane_pid,
        12345,
        "bunx",
        status,
        4121465,
        23456,
        "bunx",
        "1" * 64,
        "/nix/store/bun/bin/bun",
        1,
        2,
        agent_pid,
        34567,
        "codex",
        "3" * 64,
        "/nix/store/codex/bin/codex",
        1,
        3,
        "4" * 64,
    )


@final
class NamespaceDrainTests(unittest.TestCase):
    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    authority: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    authority_sha: str  # pyright: ignore[reportUninitializedInstanceVariable]
    private_temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    private: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    root_patch: object  # pyright: ignore[reportUninitializedInstanceVariable]
    digest_patch: object  # pyright: ignore[reportUninitializedInstanceVariable]
    receipt_patch: object  # pyright: ignore[reportUninitializedInstanceVariable]
    verifier_mock: object  # pyright: ignore[reportUninitializedInstanceVariable]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.private_temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private = Path(self.private_temporary.name)
        self.authority = self.root / "manager_mail" / "85c5dff58359-1261.txt"
        self.authority.parent.mkdir()
        self.authority.write_text(
            "Subject: Re: Close uncontrolled agents\n\n"
            "Do these now! I’m still seeing new emails from these agents that should have been closed and I don’t see mail count go down. It should be the opposite!!\n\n"
            "> On Aug 30, 2026, at 16:08, sichangheagent@gmail.com wrote:\n> \n> Added pending items:\n> \n"
            "> Close all opsmail0802 and agent_managers agents.\n"
            "> Consolidate their tasks and status and send one email summarizing them.\n"
            "> Let a new agent do mailbox compression.\n",
            encoding="utf-8",
        )
        self.authority.chmod(0o600)
        self.authority_sha = hashlib.sha256(self.authority.read_bytes()).hexdigest()
        self.root_patch = patch("omo_manager.omo_namespace_drain.TRUSTED_ROOT", self.root)
        self.digest_patch = patch("omo_manager.omo_namespace_drain.AUTHORITY_SHA256", self.authority_sha)
        self.receipt_patch = patch("omo_manager.omo_namespace_drain.TRUSTED_RECEIPT_DIR", self.private)
        self.root_patch.start()  # type: ignore[attr-defined]
        self.digest_patch.start()  # type: ignore[attr-defined]
        self.receipt_patch.start()  # type: ignore[attr-defined]
        self.verifier_mock = patch("omo_manager.omo_namespace_drain.verify_consumed_report", return_value=None).start()

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.private_temporary.cleanup()
        self.digest_patch.stop()  # pyright: ignore[reportAttributeAccessIssue]
        self.receipt_patch.stop()  # pyright: ignore[reportAttributeAccessIssue]
        self.root_patch.stop()  # pyright: ignore[reportAttributeAccessIssue]
        patch.stopall()

    def write_review(self, plan_sha: str) -> tuple[Path, str, Path, str, Path, str]:
        review = self.root / "review.json"
        reviewer = self.root / "reviewer.md"
        reviewer.write_text(task("independent:1"), encoding="utf-8")
        reviewer_sha = sha256_bytes(reviewer.read_bytes())
        review.write_bytes(
            canonical_json(
                {
                    "schema": "omo-namespace-drain-review/v1",
                    "plan_sha256": plan_sha,
                    "verdict": "PASS",
                    "reviewer": "independent:1",
                    "report_agent": "independent-reviewer",
                    "reviewer_task_sha256": reviewer_sha,
                }
            )
        )
        review.chmod(0o600)
        consumed = self.private / f"consumed-{plan_sha}.json"
        unsigned: dict[str, object] = {
            "schema": "omo-report-consumed-closure/v1",
            "terminal": True,
            "accepted": False,
            "consumed_at_unix_s": 1.0,
            "input": {"file_sha256": sha256_bytes(review.read_bytes())},
            "reason": "manager watcher consumed report; acceptance receipt unavailable",
            "recovery_residue": [],
            "replay_id": "test-replay",
            "status": "in-progress",
            "transfer_receipt": {},
        }
        consumed.write_bytes(canonical_json({**unsigned, "attestation_id": bound_receipt_id(unsigned)}))
        consumed.chmod(0o600)
        return review, sha256_bytes(review.read_bytes()), reviewer, reviewer_sha, consumed, sha256_bytes(consumed.read_bytes())

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%1", 100, 200))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="running")
    def test_shared_target_is_stopped_once_and_queues_are_preserved(self, _inspect: object, _identity: object, _panes: object) -> None:
        (self.root / "a.md").write_text(task("opsmail0802:1", ["first", "second"]), encoding="utf-8")
        (self.root / "b.md").write_text(task("opsmail0802:1", ["third"]), encoding="utf-8")
        plan = build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)
        targets = plan["targets"]
        tasks = plan["tasks"]
        assert isinstance(targets, list) and isinstance(tasks, list) and isinstance(tasks[0], dict)
        self.assertEqual(1, len(targets))
        self.assertEqual("opsmail0802:1.0", targets[0]["target"])
        self.assertEqual(("first", "second"), tasks[0]["pending_task_items"])

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=("opsmail0802:1.0",))
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%1", 100, 200))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="running")
    def test_task_alias_and_exact_namespace_pane_are_one_target(self, _inspect: object, _identity: object, _panes: object) -> None:
        (self.root / "a.md").write_text(task("opsmail0802:1"), encoding="utf-8")
        plan = build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)
        targets = plan["targets"]
        assert isinstance(targets, list) and isinstance(targets[0], dict)
        self.assertEqual(1, len(targets))
        self.assertEqual("opsmail0802:1.0", targets[0]["target"])
        self.assertEqual(("a.md",), targets[0]["tasks"])

    @patch("omo_manager.omo_namespace_drain.exact_pane_id", return_value="")
    def test_missing_exact_target_is_absent_without_tmux_alias_fallback(self, _pane: object) -> None:
        self.assertEqual("absent", inspect_target("agent_managers:11.0"))

    def test_refuses_human_namespace(self) -> None:
        with self.assertRaisesRegex(DrainError, "exactly one"):
            build_plan(self.root, ("hmanager",), self.authority, self.authority_sha)

    def test_requires_exact_two_prefix_set(self) -> None:
        for prefixes in (("opsmail0802",), ("agent_managers",), ("opsmail0802", "opsmail0802")):
            with self.subTest(prefixes=prefixes), self.assertRaisesRegex(DrainError, "exactly one"):
                build_plan(self.root, prefixes, self.authority, self.authority_sha)

    @patch("omo_manager.omo_namespace_drain.guarded_codex_stop", side_effect=RuntimeError("bound target changed"))
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%7", 100, 200))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="running")
    def test_guarded_stop_propagates_symbolic_rebind_race(self, _inspect: object, _identity: object, guarded: object) -> None:
        with self.assertRaisesRegex(RuntimeError, "bound target changed"):
            stop_target("opsmail0802:1", "%7", 100, 200)
        guarded.assert_called_once()  # pyright: ignore[reportAttributeAccessIssue]

    def test_authority_rejects_wrong_path_symlink_and_mode(self) -> None:
        wrong = self.root / "manager_mail" / "wrong.txt"
        wrong.write_bytes(self.authority.read_bytes())
        wrong.chmod(0o600)
        with self.assertRaisesRegex(DrainError, "exact trusted"):
            build_plan(self.root, ("opsmail0802", "agent_managers"), wrong, sha256_bytes(wrong.read_bytes()))
        self.authority.chmod(0o644)
        with self.assertRaisesRegex(DrainError, "owner-private"):
            build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)

    def test_reconcile_cli_is_frozen_before_runtime_handler(self) -> None:
        common = [
            "--root",
            str(self.root),
            "--code-review",
            str(self.private / "review.json"),
            "--code-review-sha256",
            "1" * 64,
            "--consumed-code-review-receipt",
            str(self.private / "consumed.json"),
            "--consumed-code-review-receipt-sha256",
            "2" * 64,
            "--binding",
            str(self.private / "binding.json"),
            "--binding-sha256",
            "3" * 64,
            "--executor",
            "executor:1",
        ]
        cases = (
            (
                "reconcile-taskless-shell",
                "close_taskless_shell",
                {"removed_pane_id": "%12", "schema": "taskless"},
            ),
            (
                "reconcile-completed-shell",
                "close_completed_shell",
                {"task": COMPLETED_SHELL_TASK, "schema": "completed"},
            ),
        )
        for command, close_name, result in cases:
            audit = self.private / f"{command}.json"
            with (
                self.subTest(command=command),
                patch(f"omo_manager.omo_namespace_drain.{close_name}", return_value=result) as close,
                patch("omo_manager.omo_namespace_drain.Path.read_bytes", side_effect=AssertionError("audit reopened")),
                patch("builtins.print"),
            ):
                self.assertEqual(1, main([command, *common, "--audit-output", str(audit)]))
                close.assert_not_called()

    def test_output_rejects_authority_alias_existing_and_public_parent(self) -> None:
        with self.assertRaisesRegex(DrainError, "new disjoint"):
            validate_new_private_output(self.authority, self.root, {self.authority})
        existing = self.private / "existing.json"
        existing.write_text("do not overwrite", encoding="utf-8")
        with self.assertRaisesRegex(DrainError, "new disjoint"):
            validate_new_private_output(existing, self.root, set())
        public = self.private / "public"
        public.mkdir(mode=0o755)
        with self.assertRaisesRegex(DrainError, "owner-private directory"):
            validate_new_private_output(public / "new.json", self.root, set())

    def test_task_write_refuses_symlink_swap(self) -> None:
        task_path = self.root / "task.md"
        original = b"original\n"
        task_path.write_bytes(original)
        moved = self.root / "moved.md"
        task_path.rename(moved)
        task_path.symlink_to(moved)
        with self.assertRaises(OSError):
            atomic_replace_task(self.root, task_path, original, b"replacement\n")
        self.assertEqual(original, moved.read_bytes())

    def test_recoverable_lifecycle_exchange_cleans_exact_crash_residue(self) -> None:
        path = self.root / "recoverable.md"
        source = b"source\n"
        replacement = b"replacement\n"
        path.write_bytes(source)
        slot_name = lifecycle_exchange_slot_name(path, "7" * 64)
        with pinned_directory(self.private) as output_directory:
            staged = prestage_lifecycle_exchange_slot(output_directory, self.root, path, source, replacement, slot_name)
            self.assertEqual(sha256_bytes(replacement), staged["replacement_sha256"])
            restaged = prestage_lifecycle_exchange_slot(output_directory, self.root, path, source, replacement, slot_name)
            self.assertEqual(staged["replacement_object"], restaged["replacement_object"])
            with self.assertRaisesRegex(DrainError, "cannot prove descriptor-bound"):
                recoverable_replace_task(output_directory, self.root, path, source, replacement, slot_name, staged)
            self.assertEqual(source, path.read_bytes())
            self.assertEqual(replacement, (self.private / slot_name).read_bytes())

        path.write_bytes(source)
        (self.private / slot_name).write_bytes(b"foreign\n")
        (self.private / slot_name).chmod(0o644)
        with pinned_directory(self.private) as output_directory, self.assertRaisesRegex(DrainError, "foreign bytes|unsafe or drifting"):
            prestage_lifecycle_exchange_slot(output_directory, self.root, path, source, replacement, slot_name)
        self.assertEqual(source, path.read_bytes())
        self.assertEqual(b"foreign\n", (self.private / slot_name).read_bytes())
        path.write_bytes(replacement)
        with pinned_directory(self.private) as output_directory, self.assertRaisesRegex(DrainError, "cannot prove descriptor-bound"):
            recoverable_replace_task(output_directory, self.root, path, source, replacement, slot_name, staged)
        self.assertEqual(replacement, path.read_bytes())
        self.assertEqual(b"foreign\n", (self.private / slot_name).read_bytes())

    def test_guarded_shell_remove_treats_unproven_rejection_as_unknown_after_attempt(self) -> None:
        expected = shell_fixture()
        attempts = (
            subprocess.CompletedProcess(["tmux"], 124, stdout=b""),
            subprocess.CompletedProcess(["tmux"], 0, stdout=b""),
            subprocess.CompletedProcess(["tmux"], 0, stdout=b"wrong\n"),
            None,
        )
        for result in attempts:
            with (
                self.subTest(result=result),
                patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=replace(expected, root=replace(expected.root, state="T"), foreground=replace(expected.foreground, state="T"))),
            ):
                self.assertEqual(
                    "unknown-after-attempt",
                    shell_removal_attempt_state("opsmail0802:0.0", expected, result, b"accepted\n", b"rejected\n", False),
                )

    def test_recovery_phase_commit_accepts_exact_eexist_and_rejects_foreign(self) -> None:
        with pinned_directory(self.private) as output_directory:
            identity: dict[str, object] = {
                "schema": "test",
                "operation": "taskless-empty-shell",
                "target": TASKLESS_SHELL_TARGET,
            }
            journal = self.private / "phase.journal"
            journal.write_bytes(b"journal\n")
            bind_recovery_phase_objects(output_directory, journal.name, "taskless-empty-shell", identity)
            self.assertEqual("prepared", recovery_phase_from_entries(output_directory, journal.name, identity))
            self.assertEqual("kill-started", transition_shell_recovery(output_directory, journal, identity, "prepared", "kill-started"))
            self.assertEqual("kill-started", recovery_phase_from_entries(output_directory, journal.name, identity))
            record = identity["phase_records"]["pane-removed"]
            committed = self.private / record["committed_name"]
            committed.write_bytes(b"foreign\n")
            committed.chmod(0o600)
            with self.assertRaisesRegex(DrainError, "foreign|object drifted|committed phase"):
                transition_shell_recovery(output_directory, journal, identity, "kill-started", "pane-removed")

    def test_audit_bound_phase_transition_requires_retained_phase_inode(self) -> None:
        audit_output = self.private / "phase-bound-audit.json"
        audit_data = canonical_json({"schema": "phase-bound"})
        with pinned_directory(self.private) as output_directory:
            prepared_recovery = prepare_shell_recovery(
                output_directory,
                self.root,
                audit_output,
                self.private / "binding.json",
                "1" * 64,
                b"binding\n",
                self.private / "review.json",
                "2" * 64,
                self.private / "consumed.json",
                "3" * 64,
                "executor:1",
                "%9",
                "taskless-empty-shell",
                TASKLESS_SHELL_TARGET,
                {"source": "test"},
                shell_fixture(),
                audit_data,
                set(),
            )
            try:
                with self.assertRaisesRegex(DrainError, "retained future phase descriptor"):
                    transition_shell_recovery(
                        output_directory,
                        prepared_recovery.journal_path,
                        prepared_recovery.identity,
                        "prepared",
                        "kill-started",
                        audit_data,
                        prepared_recovery.staged_audit_fd,
                        prepared_recovery.staged_audit_state,
                    )
                phase_record = prepared_recovery.identity["phase_records"]["kill-started"]
                phase_staged = self.private / phase_record["staged_name"]  # pyright: ignore[reportIndexIssue]
                replacement = self.private / "phase-bound-replacement"
                replacement.write_bytes(phase_staged.read_bytes())
                replacement.chmod(0o600)
                os.replace(replacement, phase_staged)
                with self.assertRaisesRegex(DrainError, "retained shell recovery kill-started phase identity drifted|staged shell recovery kill-started phase object drifted"):
                    transition_shell_recovery(
                        output_directory,
                        prepared_recovery.journal_path,
                        prepared_recovery.identity,
                        "prepared",
                        "kill-started",
                        audit_data,
                        prepared_recovery.staged_audit_fd,
                        prepared_recovery.staged_audit_state,
                        prepared_recovery.phase_objects.get("kill-started"),
                    )
            finally:
                prepared_recovery.close()

    def test_taskless_phase_generation_drift_refuses_before_pane_removal(self) -> None:
        real_prepare = namespace_drain.prepare_shell_recovery
        mutations = (
            ("audit-rewrite-restore", "audit"),
            ("kill-started-rewrite-restore", "kill-started"),
            ("kill-started-extra-hardlink", "kill-started"),
            ("pane-removed-rewrite-restore", "pane-removed"),
            ("complete-extra-hardlink", "complete"),
        )
        for index, (mutation, drift_target) in enumerate(mutations, start=1):
            with self.subTest(mutation=mutation):
                expected = shell_fixture(f"%{10 + index}", 100 + index, 200 + index)
                review = self.private / f"phase-drift-review-{index}.json"
                consumed = self.private / f"phase-drift-consumed-{index}.json"
                binding_path = self.private / f"phase-drift-binding-{index}.json"
                audit = self.private / f"phase-drift-audit-{index}.json"
                with (
                    patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                    patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
                    patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
                ):
                    result = bind_taskless_shell(self.root, review, "1" * 64, consumed, "2" * 64, binding_path, "executor:1")

                prepared_holder: dict[str, PreparedShellRecovery] = {}

                def prepare_with_phase_drift(*args: object, **kwargs: object) -> PreparedShellRecovery:
                    prepared = real_prepare(*args, **kwargs)  # pyright: ignore[reportArgumentType]
                    prepared_holder["value"] = prepared
                    if drift_target == "audit":
                        audit_data = prepared.staged_audit.read_bytes()
                        prepared.staged_audit.write_bytes(b"x" * len(audit_data))
                        prepared.staged_audit.write_bytes(audit_data)
                        os.utime(prepared.staged_audit, ns=(1_000_000_000 + index, 2_000_000_000 + index))
                        return prepared
                    phase_record = prepared.identity["phase_records"][drift_target]  # pyright: ignore[reportIndexIssue]
                    phase_staged = self.private / str(phase_record["staged_name"])  # pyright: ignore[reportIndexIssue]
                    if mutation.endswith("rewrite-restore"):
                        phase_data = phase_staged.read_bytes()
                        phase_staged.write_bytes(b"x" * len(phase_data))
                        phase_staged.write_bytes(phase_data)
                        os.utime(phase_staged, ns=(1_000_000_000 + index, 2_000_000_000 + index))
                    else:
                        os.link(phase_staged, self.private / f"phase-drift-extra-{index}")
                    return prepared

                with (
                    patch("omo_manager.omo_namespace_drain.prepare_shell_recovery", side_effect=prepare_with_phase_drift),
                    patch("omo_manager.omo_namespace_drain.reconciliation_preflight", return_value={"reviewer": "cedit:28"}),
                    patch("omo_manager.omo_namespace_drain.recovery_pane_presence", return_value="present"),
                    patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
                    patch("omo_manager.omo_namespace_drain.guarded_remove_shell") as remove,
                    patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                    self.assertRaisesRegex(DrainError, "held staged shell audit identity drifted|retained shell recovery .* phase identity drifted"),
                ):
                    close_taskless_shell(
                        self.root,
                        binding_path,
                        str(result["sha256"]),
                        review,
                        "1" * 64,
                        consumed,
                        "2" * 64,
                        audit,
                        "executor:1",
                    )
                remove.assert_not_called()
                prepared = prepared_holder["value"]
                phase_record = prepared.identity["phase_records"]["pane-removed"]  # pyright: ignore[reportIndexIssue]
                self.assertFalse((self.private / str(phase_record["committed_name"])).exists())  # pyright: ignore[reportIndexIssue]

    def test_taskless_final_pre_removal_kill_started_guard(self) -> None:
        real_prepare = namespace_drain.prepare_shell_recovery
        real_transition = namespace_drain.transition_shell_recovery
        cases = (
            "valid",
            "rewrite-restore",
            "staged-loss",
            "committed-loss",
            "both-loss",
            "extra-link",
            "mid-guard-swap",
            "journal-loss",
            "journal-replacement",
            "post-link-rewrite-restore",
        )
        for index, mutation in enumerate(cases, start=1):
            with self.subTest(mutation=mutation):
                expected = shell_fixture(f"%{30 + index}", 300 + index, 400 + index)
                review = self.private / f"final-guard-review-{index}.json"
                consumed = self.private / f"final-guard-consumed-{index}.json"
                binding_path = self.private / f"final-guard-binding-{index}.json"
                audit = self.private / f"final-guard-audit-{index}.json"
                with (
                    patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                    patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
                    patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
                ):
                    result = bind_taskless_shell(self.root, review, "1" * 64, consumed, "2" * 64, binding_path, "executor:1")

                prepared_holder: dict[str, PreparedShellRecovery] = {}

                def prepare_and_hold(*args: object, **kwargs: object) -> PreparedShellRecovery:
                    prepared = real_prepare(*args, **kwargs)  # pyright: ignore[reportArgumentType]
                    prepared_holder["value"] = prepared
                    return prepared

                def kill_started_paths() -> tuple[Path, Path]:
                    prepared = prepared_holder["value"]
                    record = prepared.identity["phase_records"]["kill-started"]  # pyright: ignore[reportIndexIssue]
                    return self.private / str(record["staged_name"]), self.private / str(record["committed_name"])  # pyright: ignore[reportIndexIssue]

                def mutate_after_kill_started() -> None:
                    staged, committed = kill_started_paths()
                    if mutation == "valid":
                        return
                    if mutation in {"rewrite-restore", "post-link-rewrite-restore"}:
                        phase_data = staged.read_bytes()
                        staged.write_bytes(b"x" * len(phase_data))
                        staged.write_bytes(phase_data)
                        os.utime(staged, ns=(3_000_000_000 + index, 4_000_000_000 + index))
                    elif mutation == "staged-loss":
                        staged.unlink()
                    elif mutation == "committed-loss":
                        committed.unlink()
                    elif mutation == "both-loss":
                        staged.unlink()
                        committed.unlink()
                    elif mutation == "extra-link":
                        os.link(staged, self.private / f"final-guard-extra-{index}")
                    elif mutation == "journal-loss":
                        prepared_holder["value"].journal_path.unlink()
                    elif mutation == "journal-replacement":
                        journal = prepared_holder["value"].journal_path
                        journal_data = journal.read_bytes()
                        journal.unlink()
                        journal.write_bytes(journal_data)
                        journal.chmod(0o600)

                def transition_then_mutate(*args: object, **kwargs: object) -> str:
                    next_phase = str(args[4])
                    result_phase = real_transition(*args, **kwargs)  # pyright: ignore[reportArgumentType]
                    if next_phase == "kill-started" and mutation not in {"mid-guard-swap", "post-link-rewrite-restore"}:
                        mutate_after_kill_started()
                    return result_phase

                real_stat = namespace_drain.os.stat
                swapped = False

                def stat_with_mid_guard_swap(path: object, *args: object, **kwargs: object) -> os.stat_result:
                    nonlocal swapped
                    result_state = real_stat(path, *args, **kwargs)
                    if mutation in {"mid-guard-swap", "post-link-rewrite-restore"} and not swapped and "value" in prepared_holder:
                        _, committed = kill_started_paths()
                        if path == committed.name and kwargs.get("dir_fd") is not None and committed.exists():
                            swapped = True
                            if mutation == "mid-guard-swap":
                                committed_data = committed.read_bytes()
                                committed.unlink()
                                committed.write_bytes(committed_data)
                                committed.chmod(0o600)
                            else:
                                staged, _ = kill_started_paths()
                                phase_data = staged.read_bytes()
                                staged.write_bytes(b"x" * len(phase_data))
                                staged.write_bytes(phase_data)
                                os.utime(staged, ns=(5_000_000_000 + index, 6_000_000_000 + index))
                    return result_state

                presence_result = "present"
                context = self.assertRaisesRegex(DrainError, "phase|identity|drifted|shell recovery|sealed phase-publication")
                with (
                    context,
                    patch("omo_manager.omo_namespace_drain.prepare_shell_recovery", side_effect=prepare_and_hold),
                    patch("omo_manager.omo_namespace_drain.transition_shell_recovery", side_effect=transition_then_mutate),
                    patch("omo_manager.omo_namespace_drain.reconciliation_preflight", return_value={"reviewer": "cedit:28"}),
                    patch("omo_manager.omo_namespace_drain.recovery_pane_presence", side_effect=presence_result if isinstance(presence_result, list) else None, return_value=presence_result if isinstance(presence_result, str) else None),
                    patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
                    patch("omo_manager.omo_namespace_drain.bound_shell_processes_exited", return_value=True),
                    patch("omo_manager.omo_namespace_drain.guarded_remove_shell") as remove,
                    patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                    patch("omo_manager.omo_namespace_drain.os.stat", side_effect=stat_with_mid_guard_swap),
                ):
                    close_taskless_shell(
                        self.root,
                        binding_path,
                        str(result["sha256"]),
                        review,
                        "1" * 64,
                        consumed,
                        "2" * 64,
                        audit,
                        "executor:1",
                    )
                remove.assert_not_called()

    def test_existing_audit_output_must_be_retained_staged_inode(self) -> None:
        audit_data = canonical_json({"schema": "audit"})
        staged = self.private / "audit.staged"
        output = self.private / "audit.json"
        staged.write_bytes(audit_data)
        staged.chmod(0o600)
        os.link(staged, output)

        def publish_with_held(output_directory: object, binding: object) -> object:
            staged_fd, staged_state = validate_pinned_object_binding(output_directory, binding, audit_data, 0o600, "staged shell audit")  # pyright: ignore[reportArgumentType]
            try:
                return publish_or_validate_staged_audit(output_directory, staged, output, audit_data, binding, staged_fd, staged_state)  # pyright: ignore[reportArgumentType]
            finally:
                os.close(staged_fd)

        with pinned_directory(self.private) as output_directory:
            staged_binding = bind_pinned_audit_object(output_directory, staged.name, audit_data)
            held = publish_with_held(output_directory, staged_binding)
            try:
                self.assertEqual(os.fstat(held.staged_fd).st_ino, os.fstat(held.published_fd).st_ino)
            finally:
                held.close()

        output.unlink()
        output.write_bytes(audit_data)
        output.chmod(0o600)
        with pinned_directory(self.private) as output_directory, self.assertRaisesRegex(DrainError, "exact held staged object|held staged shell audit identity drifted"):
            publish_with_held(output_directory, staged_binding)

        output.unlink()
        extra = self.private / "audit.extra"
        os.link(staged, extra)
        with pinned_directory(self.private) as output_directory, self.assertRaisesRegex(DrainError, "link count|held staged shell audit identity drifted"):
            publish_with_held(output_directory, staged_binding)
        extra.unlink()

        stale_stage = self.private / "stale-stage"
        real_link_held = namespace_drain.link_held_staged_audit

        def replacing_before_publish(output_directory: object, staged_fd: int, output_name: str) -> None:
            staged.rename(stale_stage)
            staged.write_bytes(audit_data)
            staged.chmod(0o600)
            real_link_held(output_directory, staged_fd, output_name)  # pyright: ignore[reportArgumentType]

        with (
            pinned_directory(self.private) as output_directory,
            patch("omo_manager.omo_namespace_drain.link_held_staged_audit", side_effect=replacing_before_publish),
            self.assertRaisesRegex(DrainError, "exact held staged object|held staged shell audit identity drifted"),
        ):
            publish_with_held(output_directory, staged_binding)

        stale_stage.unlink()
        output.unlink()
        with pinned_directory(self.private) as output_directory:
            staged_binding = bind_pinned_audit_object(output_directory, staged.name, audit_data)
        os.link(staged, output)
        os.link(staged, extra)
        with pinned_directory(self.private) as output_directory, self.assertRaisesRegex(DrainError, "exact held staged object|held staged shell audit identity drifted"):
            publish_with_held(output_directory, staged_binding)
        extra.unlink()
        output.unlink()
        os.link(staged, output)
        real_stat = namespace_drain.os.stat
        replaced = False

        def replacing_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            nonlocal replaced
            result = real_stat(path, *args, **kwargs)
            if path == "audit.json" and not replaced:
                replaced = True
                output.unlink()
                output.write_bytes(audit_data)
                output.chmod(0o600)
            return result

        with (
            pinned_directory(self.private) as output_directory,
            patch("omo_manager.omo_namespace_drain.os.stat", side_effect=replacing_stat),
            self.assertRaisesRegex(DrainError, "exact held staged object|held staged shell audit identity drifted"),
        ):
            publish_with_held(output_directory, staged_binding)

    def test_prepared_journal_rejects_staged_audit_inode_substitution(self) -> None:
        audit_output = self.private / "prepared-audit.json"
        binding_path = self.private / "binding.json"
        review_path = self.private / "review.json"
        consumed_path = self.private / "consumed.json"
        audit_data = canonical_json({"schema": "prepared-audit"})
        with pinned_directory(self.private) as output_directory:
            prepared_recovery = prepare_shell_recovery(
                output_directory,
                self.root,
                audit_output,
                binding_path,
                "1" * 64,
                b"binding\n",
                review_path,
                "2" * 64,
                consumed_path,
                "3" * 64,
                "executor:1",
                "%9",
                "taskless-empty-shell",
                TASKLESS_SHELL_TARGET,
                {"source": "test"},
                shell_fixture(),
                audit_data,
                set(),
            )
            try:
                self.assertEqual("prepared", prepared_recovery.phase)
                staged_audit = prepared_recovery.staged_audit
                with self.assertRaisesRegex(DrainError, "retained staged audit descriptor"):
                    transition_shell_recovery(
                        output_directory,
                        prepared_recovery.journal_path,
                        prepared_recovery.identity,
                        "prepared",
                        "kill-started",
                    )
                self.assertEqual(
                    "kill-started",
                    transition_shell_recovery(
                        output_directory,
                        prepared_recovery.journal_path,
                        prepared_recovery.identity,
                        "prepared",
                        "kill-started",
                        audit_data,
                        prepared_recovery.staged_audit_fd,
                        prepared_recovery.staged_audit_state,
                        prepared_recovery.phase_objects.get("kill-started"),
                    ),
                )
            finally:
                prepared_recovery.close()
            retry_recovery = prepare_shell_recovery(
                output_directory,
                self.root,
                audit_output,
                binding_path,
                "1" * 64,
                b"binding\n",
                review_path,
                "2" * 64,
                consumed_path,
                "3" * 64,
                "executor:1",
                "%9",
                "taskless-empty-shell",
                TASKLESS_SHELL_TARGET,
                {"source": "test"},
                shell_fixture(),
                audit_data,
                set(),
            )
            try:
                self.assertEqual("kill-started", retry_recovery.phase)
                self.assertIsNone(retry_recovery.staged_audit_fd)
                with self.assertRaisesRegex(DrainError, "retained staged audit descriptor"):
                    transition_shell_recovery(
                        output_directory,
                        retry_recovery.journal_path,
                        retry_recovery.identity,
                        "kill-started",
                        "pane-removed",
                    )
            finally:
                retry_recovery.close()
            replacement = self.private / "prepared-audit-replacement"
            replacement.write_bytes(audit_data)
            replacement.chmod(0o600)
            os.replace(replacement, staged_audit)
            with self.assertRaisesRegex(DrainError, "staged shell audit.*drifted"):
                prepare_shell_recovery(
                    output_directory,
                    self.root,
                    audit_output,
                    binding_path,
                    "1" * 64,
                    b"binding\n",
                    review_path,
                    "2" * 64,
                    consumed_path,
                    "3" * 64,
                    "executor:1",
                    "%9",
                    "taskless-empty-shell",
                    TASKLESS_SHELL_TARGET,
                    {"source": "test"},
                    shell_fixture(),
                    audit_data,
                    set(),
                )

    def test_task_write_preserves_owner_private_mode(self) -> None:
        task_path = self.root / "private-task.md"
        original = b"original\n"
        task_path.write_bytes(original)
        task_path.chmod(0o600)
        atomic_replace_task(self.root, task_path, original, b"replacement\n")
        self.assertEqual(0o600, task_path.stat().st_mode & 0o777)

    def test_consumed_verifier_contract_binds_reviewer_pane_and_valid_agent(self) -> None:
        self.verifier_mock.stop()  # pyright: ignore[reportAttributeAccessIssue]
        attestation = b'{"terminal":true}\n'
        verified = subprocess.CompletedProcess(["omo_report.sh"], 0, stdout=attestation, stderr=b"")
        with patch("omo_manager.omo_namespace_drain.exact_pane_id", return_value="%77"), patch("omo_manager.omo_namespace_drain.subprocess.run", return_value=verified) as run:
            verify_consumed_report(self.root / "review.json", "independent:1", "independent-reviewer", "in-progress", attestation, self.root)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertIn("independent-reviewer", command)
        self.assertNotIn("independent:1", command)
        self.assertEqual("%77", environment["TMUX_PANE"])
        self.assertEqual(str(self.root), environment["OMO_WORK_LOGS_ROOT"])
        self.verifier_mock = patch("omo_manager.omo_namespace_drain.verify_consumed_report", return_value=None).start()

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    def test_malformed_namespace_record_and_not_codex_fail_preflight(self, _panes: object) -> None:
        malformed = self.root / "bad.md"
        malformed.write_text('---\n runat: "opsmail0802:9"\n---\n', encoding="utf-8")
        with self.assertRaisesRegex(DrainError, "invalid namespace task metadata"):
            build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)
        malformed.unlink()
        (self.root / "a.md").write_text(task("opsmail0802:9"), encoding="utf-8")
        with patch("omo_manager.omo_namespace_drain.inspect_target", return_value="not_codex"):
            with self.assertRaisesRegex(DrainError, "separate reviewed lifecycle"):
                build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_drift_refuses_before_stop_or_write(self, _inspect: object, _identity: object, _panes: object) -> None:
        path = self.root / "a.md"
        path.write_text(task("agent_managers:1"), encoding="utf-8")
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review(plan_sha)
        path.write_text(task("agent_managers:1") + "drift", encoding="utf-8")
        with self.assertRaisesRegex(DrainError, "inventory drift"):
            execute(plan_path, plan_sha, review, review_sha, self.private / "ledger.json", reviewer, reviewer_sha, consumed, consumed_sha)
        self.assertFalse((self.private / "ledger.json").exists())

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%1", 100, 200))
    @patch("omo_manager.omo_namespace_drain.inspect_target", side_effect=["running", "running", "running"])
    @patch("omo_manager.omo_namespace_drain.stop_target", side_effect=DrainError("boom"))
    def test_partial_failure_records_recovery_evidence(self, _stop: object, _inspect: object, _identity: object, _panes: object) -> None:
        (self.root / "a.md").write_text(task("opsmail0802:1", ["keep"]), encoding="utf-8")
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review(plan_sha)
        ledger = self.private / "ledger.json"
        with self.assertRaisesRegex(DrainError, "boom"):
            execute(plan_path, plan_sha, review, review_sha, ledger, reviewer, reviewer_sha, consumed, consumed_sha)
        progress = ledger.with_name(f"{ledger.name}.progress.json")
        self.assertEqual("boom", json.loads(progress.read_text())["partial_failure"])
        self.assertIn("keep", ledger.read_text())

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_requires_plan_bound_review_and_suppresses_email(self, _inspect: object, _identity: object, _panes: object) -> None:
        (self.root / "a.md").write_text(task("opsmail0802:1"), encoding="utf-8")
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review("0" * 64)
        with self.assertRaisesRegex(DrainError, "exact PASS"):
            execute(plan_path, plan_sha, review, review_sha, self.private / "ledger.json", reviewer, reviewer_sha, consumed, consumed_sha)
        self.assertEqual("suppressed", json.loads(plan_path.read_text())["email_policy"])

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_forged_consumed_attestation_and_human_executor_are_rejected(self, _inspect: object, _identity: object, _panes: object) -> None:
        (self.root / "a.md").write_text(task("opsmail0802:1"), encoding="utf-8")
        plan_path = self.root / "plan-forged.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, _ = self.write_review(plan_sha)
        forged = json.loads(consumed.read_text())
        forged["attestation_id"] = "0" * 64
        consumed.write_bytes(canonical_json(forged))
        consumed_sha = sha256_bytes(consumed.read_bytes())
        with self.assertRaisesRegex(DrainError, "consumed-report provenance"):
            execute(plan_path, plan_sha, review, review_sha, self.private / "forged-ledger.json", reviewer, reviewer_sha, consumed, consumed_sha)
        with self.assertRaisesRegex(DrainError, "executor must be an exact non-Human"):
            execute(plan_path, plan_sha, review, review_sha, self.private / "human-ledger.json", reviewer, reviewer_sha, consumed, consumed_sha, "hmanager:1")

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_schema_valid_forged_closure_is_rejected_by_supported_verifier(self, _inspect: object, _identity: object, _panes: object) -> None:
        (self.root / "a.md").write_text(task("opsmail0802:1"), encoding="utf-8")
        plan_path = self.root / "plan-verifier.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review(plan_sha)
        self.verifier_mock.side_effect = DrainError("supported report verifier rejected forged closure")  # pyright: ignore[reportAttributeAccessIssue]
        with self.assertRaisesRegex(DrainError, "supported report verifier"):
            execute(plan_path, plan_sha, review, review_sha, self.private / "verifier-ledger.json", reviewer, reviewer_sha, consumed, consumed_sha)

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_successful_apply_writes_parseable_retired_records_and_completion(self, _inspect: object, _identity: object, _panes: object) -> None:
        task_path = self.root / "a.md"
        task_path.write_text(task("opsmail0802:1", ["preserve me"]), encoding="utf-8")
        plan_path = self.root / "plan.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review(plan_sha)
        ledger = self.private / "ledger.json"
        result = execute(plan_path, plan_sha, review, review_sha, ledger, reviewer, reviewer_sha, consumed, consumed_sha)
        metadata = parse_task_metadata(task_path.read_text(encoding="utf-8"), work_log_root=self.root)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("blocked", metadata.status)
        self.assertEqual("retired", metadata.runat)
        self.assertEqual((), metadata.pending_task_items)
        self.assertIn("preserve me", ledger.read_text())
        progress = json.loads(ledger.with_name(f"{ledger.name}.progress.json").read_text())
        self.assertEqual([], progress["final_active_runat"])
        self.assertEqual(1, result["task_count"])

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_successful_v2_apply_preserves_structured_history(self, _inspect: object, _identity: object, _panes: object) -> None:
        task_path = self.root / "v2.md"
        task_path.write_text(v2_task("agent_managers:1"), encoding="utf-8")
        plan_path = self.root / "plan-v2.json"
        plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
        plan_sha = sha256_bytes(plan_path.read_bytes())
        review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review(plan_sha)
        ledger = self.private / "ledger-v2.json"
        execute(plan_path, plan_sha, review, review_sha, ledger, reviewer, reviewer_sha, consumed, consumed_sha)
        metadata = parse_task_metadata(task_path.read_text(encoding="utf-8"), work_log_root=self.root)
        assert metadata is not None
        self.assertEqual("retired", metadata.runat)
        self.assertEqual("task_00000000-0000-7000-8000-000000000001", metadata.task_id)
        self.assertEqual(1, len(metadata.resolved_task_items))
        self.assertEqual("preserved evidence", metadata.resolved_task_items[0].evidence)
        self.assertEqual(2, len(metadata.blockers))

    @patch("omo_manager.omo_namespace_drain.namespace_pane_targets", return_value=())
    @patch("omo_manager.omo_namespace_drain.target_identity", return_value=("", 0, 0))
    @patch("omo_manager.omo_namespace_drain.inspect_target", return_value="absent")
    def test_v2_active_states_get_valid_resume_status(self, _inspect: object, _identity: object, _panes: object) -> None:
        for index, status in enumerate(("running", "long_running"), start=1):
            with self.subTest(status=status):
                task_path = self.root / f"v2-{status}.md"
                task_path.write_text(v2_task("agent_managers:1", status), encoding="utf-8")
                plan_path = self.root / f"plan-{status}.json"
                plan_path.write_bytes(canonical_json(build_plan(self.root, ("opsmail0802", "agent_managers"), self.authority, self.authority_sha)))
                plan_sha = sha256_bytes(plan_path.read_bytes())
                review, review_sha, reviewer, reviewer_sha, consumed, consumed_sha = self.write_review(plan_sha)
                ledger = self.private / f"ledger-{status}.json"
                execute(plan_path, plan_sha, review, review_sha, ledger, reviewer, reviewer_sha, consumed, consumed_sha)
                metadata = parse_task_metadata(task_path.read_text(encoding="utf-8"), work_log_root=self.root)
                assert metadata is not None
                self.assertEqual(status, metadata.resume_status)
                task_path.unlink()

    def test_code_review_gate_binds_current_files_task_and_consumed_route(self) -> None:
        reviewer = self.root / "ns_code_review.md"
        reviewer.write_text(task("cedit:28"), encoding="utf-8")
        review = self.private / "review.json"
        review.write_bytes(
            canonical_json(
                {
                    "schema": CODE_REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "helper_sha256": sha256_bytes(Path(__file__).parents[1].joinpath("omo_namespace_drain.py").read_bytes()),
                    "tests_sha256": sha256_bytes(TEST_PATH.read_bytes()),
                    "documentation_sha256": sha256_bytes(DOC_PATH.read_bytes()),
                    "reviewer": "cedit:28",
                    "reviewer_task": "ns_code_review.md",
                    "reviewer_task_sha256": sha256_bytes(reviewer.read_bytes()),
                    "report_agent": "code-reviewer",
                }
            )
        )
        review.chmod(0o600)
        unsigned: dict[str, object] = {
            "schema": "omo-report-consumed-closure/v1",
            "terminal": True,
            "accepted": False,
            "input": {"file_sha256": sha256_bytes(review.read_bytes())},
            "status": "in-progress",
        }
        consumed = self.private / "consumed.json"
        consumed.write_bytes(canonical_json({**unsigned, "attestation_id": bound_receipt_id(unsigned)}))
        consumed.chmod(0o600)
        result = validate_code_review(
            self.root,
            review,
            sha256_bytes(review.read_bytes()),
            consumed,
            sha256_bytes(consumed.read_bytes()),
            "executor:1",
        )
        self.assertEqual("cedit:28", result["reviewer"])
        self.verifier_mock.assert_called()  # pyright: ignore[reportAttributeAccessIssue]

    def test_shell_snapshot_binds_exact_nested_fish_prompt(self) -> None:
        expected = shell_fixture()
        root = expected.root
        foreground = expected.foreground
        assert root is not None and foreground is not None
        capture = (
            "❯                                                                                                                                                "
            "uscnsl-exxact-server /ssd1/sichangheagent/opsmail0802\n"
        ) * 2
        expected = replace(expected, capture_sha256=sha256_bytes(capture.encode()))
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="not_codex"),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%7", 101, 202)),
            patch("omo_manager.omo_namespace_drain.nested_shell_tree_snapshot", return_value=(root, foreground)),
            patch(
                "omo_manager.omo_namespace_drain.tmux_bytes",
                side_effect=(
                    b"fish\n",
                    b"\n",
                    capture.encode(),
                    b"/ssd1/sichangheagent/opsmail0802|1|202|62|2|0\n",
                ),
            ),
            patch("omo_manager.omo_namespace_drain.platform.node", return_value="uscnsl-exxact-server"),
        ):
            self.assertEqual(expected, shell_snapshot("opsmail0802:0.0", require_empty_transcript=True))
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="not_codex"),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%7", 101, 202)),
            patch("omo_manager.omo_namespace_drain.nested_shell_tree_snapshot", return_value=(root, foreground)),
            patch(
                "omo_manager.omo_namespace_drain.tmux_bytes",
                side_effect=(b"fish\n", b"\n", b"echo work\nwork\n", b"/ssd1/sichangheagent/opsmail0802|1|202|62|2|0\n"),
            ),
            self.assertRaisesRegex(DrainError, "substantive or ambiguous"),
        ):
            shell_snapshot("opsmail0802:0.0", require_empty_transcript=True)
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="not_codex"),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%7", 101, 202)),
            patch("omo_manager.omo_namespace_drain.nested_shell_tree_snapshot", return_value=(root, foreground)),
            patch(
                "omo_manager.omo_namespace_drain.tmux_bytes",
                side_effect=(b"fish\n", b"\n", capture.encode(), b"/ssd1/sichangheagent/opsmail0802|1|202|62|3|0\n"),
            ),
            self.assertRaisesRegex(DrainError, "substantive or ambiguous"),
        ):
            shell_snapshot("opsmail0802:0.0", require_empty_transcript=True)
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="not_codex"),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%7", 101, 202)),
            patch("omo_manager.omo_namespace_drain.nested_shell_tree_snapshot", side_effect=DrainError("extra grandchild")),
            patch("omo_manager.omo_namespace_drain.tmux_bytes", return_value=b"fish\n"),
            self.assertRaisesRegex(DrainError, "extra grandchild"),
        ):
            shell_snapshot("opsmail0802:0.0")

    def test_guarded_shell_remove_freezes_pid_and_kills_only_bound_pane(self) -> None:
        expected = shell_fixture()
        commands: list[list[str]] = []

        def run(arguments: list[str], _label: str) -> subprocess.CompletedProcess[bytes]:
            commands.append(arguments)
            if arguments[0] == "if-shell":
                accepted = next(part for part in arguments[5].split() if part.startswith("OMO_SHELL_REMOVE_ACCEPTED_"))
                return subprocess.CompletedProcess(arguments, 0, stdout=f"{accepted}\n".encode(), stderr=b"")
            return subprocess.CompletedProcess(arguments, 1, stdout=b"", stderr=b"missing")

        with (
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            patch("omo_manager.omo_namespace_drain.process_start_ticks", return_value=202),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%7", 101, 202)),
            patch(
                "omo_manager.omo_namespace_drain.nested_shell_tree_snapshot",
                return_value=(expected.root, expected.foreground),
            ),
            patch("omo_manager.omo_namespace_drain.pidfd_open", return_value=9),
            patch("omo_manager.omo_namespace_drain.os.close"),
            patch("omo_manager.omo_namespace_drain.pidfd_signal") as send_signal,
            patch("omo_manager.omo_namespace_drain.wait_pidfd_exits", return_value=True),
            patch("omo_manager.omo_namespace_drain.Path.read_text", return_value="101 (zsh) T 1 2 3"),
            patch("omo_manager.omo_namespace_drain.tmux_result", side_effect=run),
            patch("omo_manager.omo_namespace_drain.exact_pane_id", return_value=""),
        ):
            guarded_remove_shell("opsmail0802:0.0", expected)
        guarded = next(command for command in commands if command[0] == "if-shell")
        self.assertIn("kill-pane -t %7", guarded[5])
        self.assertNotIn("kill-session", guarded[5])
        self.assertEqual(4, send_signal.call_count)

    def test_taskless_binding_and_reconcile_refuse_any_task_record(self) -> None:
        expected = shell_fixture("%4", 44, 55)
        review = self.private / "unused-review.json"
        consumed = self.private / "unused-consumed.json"
        binding_path = self.private / "taskless-binding.json"
        audit = self.private / "taskless-audit.json"
        pane_present = True

        def remove_shell(*_args: object, **_kwargs: object) -> None:
            nonlocal pane_present
            pane_present = False

        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            patch(
                "omo_manager.omo_namespace_drain.recovery_pane_presence",
                side_effect=lambda *_args: "present" if pane_present else "absent",
            ),
            patch("omo_manager.omo_namespace_drain.bound_shell_processes_exited", return_value=True),
            patch("omo_manager.omo_namespace_drain.guarded_remove_shell", side_effect=remove_shell) as remove,
        ):
            result = bind_taskless_shell(self.root, review, "1" * 64, consumed, "2" * 64, binding_path, "executor:1")
            with self.assertRaisesRegex(DrainError, "sealed phase-publication"):
                close_taskless_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    audit,
                    "executor:1",
                )
            (self.root / "rogue.md").write_text(task("opsmail0802:0"), encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "zero task records"):
                close_taskless_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "second-audit.json",
                    "executor:1",
                )
            remove.assert_not_called()
        (self.root / "rogue.md").unlink()
        malformed = self.root / "malformed.md"
        malformed.write_text('---\n  "runat" : !!str opsmail0802:00\n  broken\n---\n', encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "zero-padded-apply-audit.json",
                "executor:1",
            )
        malformed.write_text("---\n  'runat' : !!str opsmail0802:00.00\n  broken\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "malformed-binding.json",
                "executor:1",
            )
        malformed.write_text("---\nrunat: !!str >-\n  opsmail0802:00.00\nbroken\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "folded-malformed-binding.json",
                "executor:1",
            )
        malformed.write_text("---\nrunat: !!str >-\n  opsmail0802:00.00\nbroken\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "folded-malformed-audit.json",
                "executor:1",
            )
        malformed.write_text("---\ntarget: &target !!str opsmail0802:00.00\nrunat: *target\nunknown: invalid\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "alias-malformed-binding.json",
                "executor:1",
            )
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "alias-malformed-audit.json",
                "executor:1",
            )
        malformed.write_text("---\nrunat: >-\n\topsmail0802:00.00\nunknown: invalid\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "tab-malformed-binding.json",
                "executor:1",
            )
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "tab-malformed-audit.json",
                "executor:1",
            )
        for label, key, continuation in (
            ("bare", "runat:", "\t"),
            ("tagged", "runat: !!str", "\t"),
            ("indented", "  runat: >-", "  \t"),
            ("tab-key", "\trunat: >-", "\t\t"),
            ("mixed-tab-key", "  \trunat: >-", "  \t\t"),
        ):
            malformed.write_text(f"---\n{key}\n{continuation}opsmail0802:00.00\nunknown: invalid\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
                patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
                self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
            ):
                bind_taskless_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / f"tab-{label}-malformed-binding.json",
                    "executor:1",
                )
            with (
                patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
                self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
            ):
                close_taskless_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / f"tab-{label}-malformed-audit.json",
                    "executor:1",
                )
        for label, declaration in (("plain", "? runat"), ("tagged", "? !!str runat")):
            malformed.write_text(f"---\n{declaration}\n: >-\n  opsmail0802:00.00\nbroken\n---\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
                patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
                self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
            ):
                bind_taskless_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / f"explicit-{label}-key-malformed-binding.json",
                    "executor:1",
                )
            with (
                patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
                patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
                self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
            ):
                close_taskless_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / f"explicit-{label}-key-malformed-audit.json",
                    "executor:1",
                )
        malformed.write_text("---\n!!str runat: >-\n  opsmail0802:00.00\nbroken\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "tagged-implicit-key-malformed-binding.json",
                "executor:1",
            )
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "tagged-implicit-key-malformed-audit.json",
                "executor:1",
            )
        malformed.write_text('---\n"run\\u0061t": "opsmail0802\\u003a00.00"\nbroken\n---\n', encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "escaped-key-malformed-binding.json",
                "executor:1",
            )
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "escaped-key-malformed-audit.json",
                "executor:1",
            )
        malformed.write_text("---\ntemplate: &target {runat: opsmail0802:00.00}\n<<: *target\nunknown: invalid\n---\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            bind_taskless_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "merge-malformed-binding.json",
                "executor:1",
            )
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "invalid task record claims exact target"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "merge-malformed-audit.json",
                "executor:1",
            )

    def test_reopened_kill_started_recovery_refuses_before_pane_removal(self) -> None:
        expected = shell_fixture("%4", 44, 55)
        review = self.private / "unused-review.json"
        consumed = self.private / "unused-consumed.json"
        binding_path = self.private / "taskless-binding.json"
        audit = self.private / "taskless-audit.json"
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
        ):
            result = bind_taskless_shell(self.root, review, "1" * 64, consumed, "2" * 64, binding_path, "executor:1")
        binding_data = binding_path.read_bytes()
        recovery_identity_value = {
            "audit": {
                "path": str(audit.absolute()),
                "sha256": sha256_bytes(canonical_json({"schema": "omo-namespace-drain-taskless-shell/v1"})),
                "staged_path": str(self.private / "taskless-audit.staged"),
                "staged_object": {"name": "taskless-audit.staged", "sha256": "0" * 64, "object": {}},
            }
        }
        reopened = PreparedShellRecovery(
            self.private / "taskless-audit.staged",
            self.private / "taskless-audit.journal",
            recovery_identity_value,
            "kill-started",
            None,
            None,
            None,
            None,
            {},
        )
        with (
            patch("omo_manager.omo_namespace_drain.probe_shell_recovery", return_value=("kill-started", binding_data, recovery_identity_value)),
            patch("omo_manager.omo_namespace_drain.prepare_shell_recovery", return_value=reopened),
            patch("omo_manager.omo_namespace_drain.reconciliation_preflight", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.recovery_pane_presence", return_value="present") as presence,
            patch("omo_manager.omo_namespace_drain.guarded_remove_shell") as remove,
            patch("omo_manager.omo_namespace_drain.bound_shell_processes_exited", return_value=True),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            self.assertRaisesRegex(DrainError, "retained staged audit descriptor"),
        ):
            close_taskless_shell(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                audit,
                "executor:1",
            )
        remove.assert_not_called()
        presence.assert_called_once()

    def test_completed_shell_reconcile_is_exact_no_mail_lifecycle(self) -> None:
        task_path = self.root / COMPLETED_SHELL_TASK
        todo_path = self.root / "TODO.md"
        owner_path = self.root / "owner.md"
        task_path.write_text(completed_task(), encoding="utf-8")
        todo_path.write_text(todo_for_completed_task(), encoding="utf-8")
        owner_path.write_text(manager_task(), encoding="utf-8")
        _ = subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "fixture"], check=True)
        plan = build_completion_email(self.root, task_path, task_path.read_text(encoding="utf-8"), "task done")
        assert plan is not None
        state = self.private / "completion-state"
        receipt = state / "completion-email-delivered" / plan.key
        receipt.parent.mkdir(parents=True)
        receipt.write_text(f"{plan.target}\t{task_path.name}\n", encoding="utf-8")
        receipt.chmod(0o600)
        expected = shell_fixture(COMPLETED_SHELL_PANE, 404, 505)
        review = self.private / "unused-review-completed.json"
        consumed = self.private / "unused-consumed-completed.json"
        binding_path = self.private / "completed-binding.json"
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shell_snapshot", return_value=expected),
            patch("omo_manager.omo_namespace_drain.guarded_remove_shell") as remove,
            patch("omo_manager.omo_namespace_drain.exact_pane_id", return_value="%owner"),
            patch("omo_manager.omo_namespace_drain.completion_email_state_dir", return_value=state),
        ):
            malformed_owner = self.root / "malformed-owner.md"
            malformed_owner.write_text('---\n  "runat" : !!str owner:01\n  broken\n---\n', encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "invalid record conflicts with manager owner"):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "conflict-binding.json",
                    "executor:1",
                )
            malformed_owner.unlink()
            alias_task = self.root / "alias-active.md"
            alias_task.write_text("---\nrunat: !!str >-\n  agent_managers:039.00\nbroken\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "invalid task record claims exact target"):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "folded-alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text("---\ntarget: &target !!str agent_managers:039.00\nrunat: *target\nunknown: invalid\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "invalid task record claims exact target"):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "anchor-alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text("---\nrunat: >-\n\tagent_managers:039.00\nunknown: invalid\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "tab-alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            for label, key, continuation in (
                ("bare", "runat:", "\t"),
                ("tagged", "runat: !!str", "\t"),
                ("indented", "  runat: >-", "  \t"),
                ("tab-key", "\trunat: >-", "\t\t"),
                ("mixed-tab-key", "  \trunat: >-", "  \t\t"),
            ):
                alias_task.write_text(f"---\n{key}\n{continuation}agent_managers:039.00\nunknown: invalid\n---\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    DrainError,
                    "invalid (?:task record claims exact target|record conflicts with manager owner)",
                ):
                    bind_completed_shell(
                        self.root,
                        review,
                        "1" * 64,
                        consumed,
                        "2" * 64,
                        receipt,
                        sha256_bytes(receipt.read_bytes()),
                        self.private / f"tab-{label}-alias-conflict-binding.json",
                        "executor:1",
                    )
                alias_task.unlink()
            for label, declaration in (("plain", "? runat"), ("tagged", "? !!str runat")):
                alias_task.write_text(f"---\n{declaration}\n: >-\n  agent_managers:039.00\nbroken\n---\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    DrainError,
                    "invalid (?:task record claims exact target|record conflicts with manager owner)",
                ):
                    bind_completed_shell(
                        self.root,
                        review,
                        "1" * 64,
                        consumed,
                        "2" * 64,
                        receipt,
                        sha256_bytes(receipt.read_bytes()),
                        self.private / f"explicit-{label}-key-alias-conflict-binding.json",
                        "executor:1",
                    )
                alias_task.unlink()
            alias_task.write_text("---\n!!str runat: >-\n  agent_managers:039.00\nbroken\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "tagged-implicit-key-alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text('---\n"run\\u0061t": "agent_managers\\u003a039.00"\nbroken\n---\n', encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "escaped-key-alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text("---\ntemplate: &target {runat: agent_managers:039.00}\n<<: *target\nunknown: invalid\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "merge-alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text(task("agent_managers:039.00", status="running"), encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "exactly one authoritative active task"):
                bind_completed_shell(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    receipt,
                    sha256_bytes(receipt.read_bytes()),
                    self.private / "alias-conflict-binding.json",
                    "executor:1",
                )
            alias_task.unlink()
            result = bind_completed_shell(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                receipt,
                sha256_bytes(receipt.read_bytes()),
                binding_path,
                "executor:1",
            )
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            task_replacement, todo_replacement = completed_task_replacements(
                self.root,
                task_path,
                task_path.read_text(encoding="utf-8"),
                todo_path.read_text(encoding="utf-8"),
                datetime.fromisoformat(str(binding["prepared_at"])),
                str(binding["close_note"]),
            )
            task_path.write_text(task_replacement, encoding="utf-8")
            mixed_audit = self.private / "mixed-no-journal-audit.json"
            with self.assertRaisesRegex(DrainError, "requires exact source lifecycle bytes before prepared"):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    mixed_audit,
                    "executor:1",
                )
            self.assertFalse(mixed_audit.exists())
            self.assertFalse(any(self.private.glob(".mixed-no-journal-audit.json.namespace-drain-*")))
            task_path.write_text(completed_task(), encoding="utf-8")
            malformed_owner.write_text("---\n  'runat' : !!str owner:01.00\n  broken\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "invalid record conflicts with manager owner"):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "owner-conflict-audit.json",
                    "executor:1",
                )
            malformed_owner.unlink()
            alias_task.write_text("---\nrunat: !!str >-\n  agent_managers:039.00\nbroken\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "invalid task record claims exact target"):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "folded-alias-owner-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text("---\ntarget: &target !!str agent_managers:039.00\nrunat: *target\nunknown: invalid\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "invalid task record claims exact target"):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "anchor-alias-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text("---\nrunat: >-\n\tagent_managers:039.00\nunknown: invalid\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "tab-alias-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            for label, key, continuation in (
                ("bare", "runat:", "\t"),
                ("tagged", "runat: !!str", "\t"),
                ("indented", "  runat: >-", "  \t"),
                ("tab-key", "\trunat: >-", "\t\t"),
                ("mixed-tab-key", "  \trunat: >-", "  \t\t"),
            ):
                alias_task.write_text(f"---\n{key}\n{continuation}agent_managers:039.00\nunknown: invalid\n---\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    DrainError,
                    "invalid (?:task record claims exact target|record conflicts with manager owner)",
                ):
                    close_completed_shell(
                        self.root,
                        binding_path,
                        str(result["sha256"]),
                        review,
                        "1" * 64,
                        consumed,
                        "2" * 64,
                        self.private / f"tab-{label}-alias-conflict-audit.json",
                        "executor:1",
                    )
                alias_task.unlink()
            for label, declaration in (("plain", "? runat"), ("tagged", "? !!str runat")):
                alias_task.write_text(f"---\n{declaration}\n: >-\n  agent_managers:039.00\nbroken\n---\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    DrainError,
                    "invalid (?:task record claims exact target|record conflicts with manager owner)",
                ):
                    close_completed_shell(
                        self.root,
                        binding_path,
                        str(result["sha256"]),
                        review,
                        "1" * 64,
                        consumed,
                        "2" * 64,
                        self.private / f"explicit-{label}-key-alias-conflict-audit.json",
                        "executor:1",
                    )
                alias_task.unlink()
            alias_task.write_text("---\n!!str runat: >-\n  agent_managers:039.00\nbroken\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "tagged-implicit-key-alias-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text('---\n"run\\u0061t": "agent_managers\\u003a039.00"\nbroken\n---\n', encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "escaped-key-alias-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text("---\ntemplate: &target {runat: agent_managers:039.00}\n<<: *target\nunknown: invalid\n---\n", encoding="utf-8")
            with self.assertRaisesRegex(
                DrainError,
                "invalid (?:task record claims exact target|record conflicts with manager owner)",
            ):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "merge-alias-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            alias_task.write_text(task("agent_managers:039.00", status="running"), encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "active-task ownership"):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "alias-owner-conflict-audit.json",
                    "executor:1",
                )
            alias_task.unlink()
            foreign = self.root / "foreign-drift.txt"
            foreign.write_text("must be preserved\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "repository or index state drifted"):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "drift-audit.json",
                    "executor:1",
                )
            remove.assert_not_called()
            foreign.unlink()
            original_task = task_path.read_bytes()
            original_todo = todo_path.read_bytes()

            with (
                patch("omo_manager.omo_namespace_drain.recovery_pane_presence", side_effect=["present", "absent"]),
                patch("omo_manager.omo_namespace_drain.bound_shell_processes_exited", return_value=True),
                patch("omo_manager.omo_namespace_drain.prepare_shell_recovery") as prepare_recovery,
                patch("omo_manager.omo_namespace_drain.prestage_lifecycle_exchange_slot") as prestage_slot,
                self.assertRaisesRegex(DrainError, "cannot prove descriptor-bound"),
            ):
                close_completed_shell(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "rollback-audit.json",
                    "executor:1",
                )
            prepare_recovery.assert_not_called()
            prestage_slot.assert_not_called()
            self.assertEqual(original_task, task_path.read_bytes())
            self.assertEqual(original_todo, todo_path.read_bytes())
            self.assertFalse((self.private / "rollback-audit.json").exists())
            staged_audit, journal = recovery_paths(
                self.private / "rollback-audit.json",
                str(result["sha256"]),
                "completed-task-shell",
            )
            self.assertFalse(staged_audit.exists())
            self.assertFalse(journal.exists())
            self.assertFalse((self.private / f"{journal.name}.prepared").exists())
            for phase in ("kill-started", "pane-removed", "files-exchanged", "complete"):
                self.assertFalse((self.private / f"{journal.name}.{phase}").exists())
                self.assertFalse((self.private / recovery_phase_staged_name(journal.name, phase)).exists())
            self.assertFalse((self.private / lifecycle_exchange_slot_name(task_path, str(result["sha256"]))).exists())
            self.assertFalse((self.private / lifecycle_exchange_slot_name(todo_path, str(result["sha256"]))).exists())
        metadata = parse_task_metadata(task_path.read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("blocked", metadata.status)
        self.assertNotIn(COMPLETED_SHELL_TASK, todo_path.read_text(encoding="utf-8").partition("previous:")[2])
        remove.assert_not_called()

    def test_completed_replacement_rejects_queue_and_preserves_body(self) -> None:
        task_path = self.root / COMPLETED_SHELL_TASK
        text = completed_task()
        todo = todo_for_completed_task()
        replacement, moved_todo = completed_task_replacements(self.root, task_path, text, todo, datetime(2026, 8, 30, tzinfo=UTC))
        self.assertIn(COMPLETED_SHELL_MESSAGE_ID, replacement)
        self.assertIn(COMPLETED_SHELL_TASK, moved_todo.partition("previous:")[2])
        queued = text.replace("pending_task_items: []", "pending_task_items:\n  - keep")
        with self.assertRaisesRegex(DrainError, "queue-empty"):
            completed_task_replacements(self.root, task_path, queued, todo, datetime(2026, 8, 30, tzinfo=UTC))

    def bind_absent_manager_fixture(self) -> tuple[Path, dict[str, object], Path, Path]:
        task_path = self.root / "mail_archive_ops_submgr_0802.md"
        todo_path = self.root / "TODO.md"
        task_path.write_text(absent_manager_task(), encoding="utf-8")
        todo_path.write_text(absent_manager_todo(), encoding="utf-8")
        review, _, _reviewer, _reviewer_sha, consumed, _consumed_sha = self.write_review("1" * 64)
        binding_path = self.private / "absent-manager-binding.json"
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch(
                "omo_manager.omo_namespace_drain.require_absent_history_target",
                return_value={
                    "target": "opsmail0802:1",
                    "canonical_target": "opsmail0802:1.0",
                    "state": "absent",
                    "pane_inventory_sha256": "0" * 64,
                },
            ),
            patch("omo_manager.omo_namespace_drain.repository_dirty_snapshot", return_value={"schema": "omo-namespace-drain-dirty-manifest/v1", "entries": []}),
        ):
            result = bind_absent_manager_history(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                binding_path,
                "executor:1",
                task_path.name,
                sha256_bytes(task_path.read_bytes()),
                "opsmail0802:1",
            )
        return binding_path, result, review, consumed

    def test_absent_manager_history_fails_closed_before_public_mutation_and_mail(self) -> None:
        binding_path, result, review, consumed = self.bind_absent_manager_fixture()
        task_path = self.root / "mail_archive_ops_submgr_0802.md"
        todo_path = self.root / "TODO.md"
        original_task = task_path.read_bytes()
        original_todo = todo_path.read_bytes()
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch(
                "omo_manager.omo_namespace_drain.require_absent_history_target",
                return_value={
                    "target": "opsmail0802:1",
                    "canonical_target": "opsmail0802:1.0",
                    "state": "absent",
                    "pane_inventory_sha256": "0" * 64,
                },
            ),
            patch("omo_manager.omo_namespace_drain.validate_dirty_snapshot", return_value={"schema": "omo-namespace-drain-dirty-manifest/v1", "entries": []}),
            patch("omo_manager.omo_namespace_drain.guarded_remove_shell") as remove,
            patch("omo_manager.omo_namespace_drain.guarded_codex_stop") as stop,
            patch("omo_manager.omo_namespace_drain.build_completion_email") as mail,
            patch("omo_manager.omo_namespace_drain.atomic_replace_task") as replace_task,
            self.assertRaisesRegex(DrainError, "descriptor-bound public file compare-and-swap"),
        ):
            close_absent_manager_history(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                self.private / "absent-manager-audit.json",
                "executor:1",
            )
        metadata = parse_task_metadata(original_task.decode("utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("long_running", metadata.status)
        self.assertEqual("opsmail0802:1", metadata.runat)
        self.assertEqual("wl:3", metadata.managerat)
        self.assertTrue(metadata.is_manager)
        self.assertEqual(
            (
                "Resolve only exact untracked empty shell opsmail0802:0.0 through a supported taskless exact-target disposition.",
                "Preserve ordered custody evidence.",
            ),
            metadata.pending_task_items,
        )
        self.assertEqual(original_task, task_path.read_bytes())
        self.assertEqual(original_todo, todo_path.read_bytes())
        self.assertFalse((self.private / "absent-manager-audit.json").exists())
        replace_task.assert_not_called()
        remove.assert_not_called()
        stop.assert_not_called()
        mail.assert_not_called()

    def test_absent_manager_history_rejects_duplicate_todo_and_human_target(self) -> None:
        task_path = self.root / "mail_archive_ops_submgr_0802.md"
        todo_path = self.root / "TODO.md"
        task_path.write_text(absent_manager_task("hfoo:1"), encoding="utf-8")
        todo_path.write_text(absent_manager_todo("hfoo:1"), encoding="utf-8")
        review, _, _reviewer, _reviewer_sha, consumed, _consumed_sha = self.write_review("1" * 64)
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            self.assertRaisesRegex(DrainError, "Human-owned"),
        ):
            bind_absent_manager_history(self.root, review, "1" * 64, consumed, "2" * 64, self.private / "bad-human.json", "executor:1", task_path.name, sha256_bytes(task_path.read_bytes()), "hfoo:1")
        task_path.write_text(absent_manager_task(), encoding="utf-8")
        todo_path.write_text(absent_manager_todo() + "mail_archive_ops_submgr_0802.md opsmail0802:1\n", encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch(
                "omo_manager.omo_namespace_drain.require_absent_history_target",
                return_value={
                    "target": "opsmail0802:1",
                    "canonical_target": "opsmail0802:1.0",
                    "state": "absent",
                    "pane_inventory_sha256": "0" * 64,
                },
            ),
            patch("omo_manager.omo_namespace_drain.repository_dirty_snapshot", return_value={"schema": "omo-namespace-drain-dirty-manifest/v1", "entries": []}),
            self.assertRaisesRegex(DrainError, "exactly one current TODO row"),
        ):
            bind_absent_manager_history(self.root, review, "1" * 64, consumed, "2" * 64, self.private / "bad-todo.json", "executor:1", task_path.name, sha256_bytes(task_path.read_bytes()), "opsmail0802:1")

    def test_absent_manager_history_refuses_live_target_and_reconcile_drift_before_mutation(self) -> None:
        task_path = self.root / "mail_archive_ops_submgr_0802.md"
        todo_path = self.root / "TODO.md"
        task_path.write_text(absent_manager_task(), encoding="utf-8")
        todo_path.write_text(absent_manager_todo(), encoding="utf-8")
        review, _, _reviewer, _reviewer_sha, consumed, _consumed_sha = self.write_review("1" * 64)
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch("omo_manager.omo_namespace_drain.require_absent_history_target", side_effect=DrainError("historical manager target is live or rebound")),
            self.assertRaisesRegex(DrainError, "live or rebound"),
        ):
            bind_absent_manager_history(self.root, review, "1" * 64, consumed, "2" * 64, self.private / "live.json", "executor:1", task_path.name, sha256_bytes(task_path.read_bytes()), "opsmail0802:1")
        binding_path, result, review, consumed = self.bind_absent_manager_fixture()
        task_path.write_text(absent_manager_task(pending=["different"]), encoding="utf-8")
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch("omo_manager.omo_namespace_drain.require_absent_history_target") as absence,
            patch("omo_manager.omo_namespace_drain.validate_dirty_snapshot", return_value={"schema": "omo-namespace-drain-dirty-manifest/v1", "entries": []}),
            patch("omo_manager.omo_namespace_drain.atomic_replace_task") as replace_task,
            self.assertRaisesRegex(DrainError, "bytes drifted"),
        ):
            close_absent_manager_history(self.root, binding_path, str(result["sha256"]), review, "1" * 64, consumed, "2" * 64, self.private / "drift-audit.json", "executor:1")
        replace_task.assert_not_called()
        absence.assert_not_called()

    def test_absent_manager_history_never_enters_unsafe_public_replace_path(self) -> None:
        binding_path, result, review, consumed = self.bind_absent_manager_fixture()
        task_path = self.root / "mail_archive_ops_submgr_0802.md"
        todo_path = self.root / "TODO.md"
        original_task = task_path.read_bytes()
        original_todo = todo_path.read_bytes()
        with (
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch(
                "omo_manager.omo_namespace_drain.require_absent_history_target",
                return_value={
                    "target": "opsmail0802:1",
                    "canonical_target": "opsmail0802:1.0",
                    "state": "absent",
                    "pane_inventory_sha256": "0" * 64,
                },
            ),
            patch("omo_manager.omo_namespace_drain.validate_dirty_snapshot", return_value={"schema": "omo-namespace-drain-dirty-manifest/v1", "entries": []}),
            patch("omo_manager.omo_namespace_drain.atomic_replace_task") as replace_task,
            self.assertRaisesRegex(DrainError, ABSENT_MANAGER_PUBLIC_MUTATION_BLOCKER),
        ):
            close_absent_manager_history(self.root, binding_path, str(result["sha256"]), review, "1" * 64, consumed, "2" * 64, self.private / "rollback-audit.json", "executor:1")
        self.assertEqual(original_task, task_path.read_bytes())
        self.assertEqual(original_todo, todo_path.read_bytes())
        replace_task.assert_not_called()

    def test_unslop_replacement_accepts_only_exact_completed_task(self) -> None:
        accepted = unslop_task().encode()
        with patch("omo_manager.omo_namespace_drain.UNSLOP_TASK_SHA256", sha256_bytes(accepted)):
            replacement = unslop_task_replacement(self.root, accepted)
        metadata = parse_task_metadata(replacement.decode(), self.root)
        assert metadata is not None
        self.assertEqual("done", metadata.status)
        self.assertFalse(metadata.blocked_on)
        self.assertIn(UNSLOP_ACCEPTED_EVIDENCE, replacement.decode())
        rejected = {
            "pending queue": accepted.replace(b"pending_task_items: []", b"pending_task_items:\n  - open"),
            "unaccepted result": accepted.replace(UNSLOP_ACCEPTED_EVIDENCE.encode(), b"result not accepted"),
            "different status": accepted.replace(f"status: blocked\nblocked_on: {UNSLOP_BLOCKER}".encode(), b"status: running"),
            "manager type": accepted.replace(b"is_manager: false", b"is_manager: true"),
            "different target": accepted.replace(f"runat: {UNSLOP_TARGET}".encode(), b"runat: wl:2"),
        }
        for label, source in rejected.items():
            with self.subTest(label=label), patch("omo_manager.omo_namespace_drain.UNSLOP_TASK_SHA256", sha256_bytes(source)):
                with self.assertRaisesRegex(DrainError, "exact accepted queue-empty completion state"):
                    unslop_task_replacement(self.root, source)

    def test_unslop_shared_pane_binds_ready_managed_descendant_and_rejects_exit(self) -> None:
        agent = (
            4121465,
            23456,
            "bunx",
            "1" * 64,
            "/nix/store/bun/bin/bun",
            1,
            2,
            4121489,
            34567,
            "codex",
            "3" * 64,
            "/nix/store/codex/bin/codex",
            1,
            3,
            "4" * 64,
        )
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="ready"),
            patch(
                "omo_manager.omo_namespace_drain.target_identity",
                return_value=(UNSLOP_PANE, 806864, 12345),
            ),
            patch(
                "omo_manager.omo_namespace_drain.tmux_bytes",
                return_value=f"{UNSLOP_PANE}|806864|bunx\n".encode(),
            ),
            patch(
                "omo_manager.omo_namespace_drain.managed_agent_descendant",
                return_value=agent,
            ),
        ):
            self.assertEqual(shared_snapshot(), shared_pane_snapshot())
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="not_codex"),
            patch("omo_manager.omo_namespace_drain.managed_agent_descendant") as descendant,
            self.assertRaisesRegex(DrainError, "not a live managed agent"),
        ):
            shared_pane_snapshot()
        descendant.assert_not_called()
        with (
            patch("omo_manager.omo_namespace_drain.inspect_target", return_value="ready"),
            patch(
                "omo_manager.omo_namespace_drain.target_identity",
                return_value=(UNSLOP_PANE, 806864, 12345),
            ),
            patch(
                "omo_manager.omo_namespace_drain.tmux_bytes",
                return_value=f"{UNSLOP_PANE}|806864|bunx\n".encode(),
            ),
            patch(
                "omo_manager.omo_namespace_drain.managed_agent_descendant",
                side_effect=[agent, DrainError("Unslop shared pane has no live managed-agent descendant")],
            ),
            self.assertRaisesRegex(DrainError, "no live managed-agent descendant"),
        ):
            shared_pane_snapshot()

    def test_unslop_managed_descendant_requires_native_codex_and_stable_lineage(self) -> None:
        launcher = (
            4121465,
            23456,
            "bunx",
            "1" * 64,
            "/nix/store/bun/bin/bun",
            1,
            2,
        )
        native = (
            4121489,
            34567,
            "codex",
            "3" * 64,
            "/nix/store/codex/bin/codex",
            1,
            3,
        )
        children = {
            806864: (806868,),
            806868: (4121465,),
            4121465: (4121489,),
            4121489: (),
        }
        starts = {
            806864: 12345,
            806868: 12346,
            4121465: 23456,
            4121489: 34567,
        }

        def process(pid: int, expected: str) -> tuple[int, int, str, str, str, int, int] | None:
            if (pid, expected) == (4121465, "bunx"):
                return launcher
            if (pid, expected) == (4121489, "codex"):
                return native
            return None

        with (
            patch(
                "omo_manager.omo_namespace_drain.shell_children",
                side_effect=lambda pid: children[pid],
            ),
            patch(
                "omo_manager.omo_namespace_drain.managed_agent_process_snapshot",
                side_effect=process,
            ),
            patch(
                "omo_manager.omo_namespace_drain.process_start_ticks",
                side_effect=lambda pid: starts.get(pid),
            ),
        ):
            selected = managed_agent_descendant(806864, "bunx")
        self.assertEqual((*launcher, *native), selected[:-1])
        self.assertRegex(selected[-1], r"^[0-9a-f]{64}$")
        exited_children = {**children, 4121465: ()}
        with (
            patch(
                "omo_manager.omo_namespace_drain.shell_children",
                side_effect=lambda pid: exited_children[pid],
            ),
            patch(
                "omo_manager.omo_namespace_drain.managed_agent_process_snapshot",
                side_effect=process,
            ),
            self.assertRaisesRegex(DrainError, "one exact Codex descendant"),
        ):
            managed_agent_descendant(806864, "bunx")

    def test_unslop_authority_requires_exact_source_and_authoritative_envelope(self) -> None:
        self.authority.parent.chmod(0o700)
        with self.assertRaisesRegex(DrainError, "not configured from an exact Human-instruction injection"):
            validate_unslop_authority(self.root)
        with (
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            self.assertRaisesRegex(DrainError, "not configured from an exact Human-instruction injection"),
        ):
            bind_unslop_done(
                self.root,
                self.private / "review.json",
                "1" * 64,
                self.private / "consumed.json",
                "2" * 64,
                self.private / "graph-cas.json",
                "3" * 64,
                self.private / "must-not-bind.json",
                "executor:1",
            )
        source1261_envelope = self.root / "source1261-envelope.md"
        source1261_excerpt = "".join(self.authority.read_text(encoding="utf-8").splitlines(keepends=True)[2:11])
        source1261_envelope.write_text(
            f'<human_instruction authoritative="true" source="manager_mail/85c5dff58359-1261.txt:3-11">\n{source1261_excerpt}</human_instruction>\n',
            encoding="utf-8",
        )
        with (
            patch(
                "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE",
                "manager_mail/85c5dff58359-1261.txt",
            ),
            patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_LINES", (3, 11)),
            patch(
                "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE_SHA256",
                sha256_bytes(self.authority.read_bytes()),
            ),
            patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_ENVELOPE", "source1261-envelope.md"),
            patch(
                "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_ENVELOPE_SHA256",
                sha256_bytes(source1261_envelope.read_bytes()),
            ),
            self.assertRaisesRegex(DrainError, "exact Unslop instruction"),
        ):
            validate_unslop_authority(self.root)
        authority = self.authority.parent / "unslop-authority.txt"
        authority.write_text(
            f"Subject: exact authorization\n\n{UNSLOP_AUTHORITY_TEXT}\n",
            encoding="utf-8",
        )
        authority.chmod(0o600)
        local_only = self.root / "local-only-envelope.md"
        local_only.write_text(f"agent says: {UNSLOP_AUTHORITY_TEXT}\n", encoding="utf-8")
        authority_source = "manager_mail/unslop-authority.txt"

        def validate_with_configuration(envelope: Path, envelope_sha256: str | None = None) -> dict[str, str]:
            with ExitStack() as stack:
                stack.enter_context(patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE", authority_source))
                stack.enter_context(patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_LINES", (3, 3)))
                stack.enter_context(
                    patch(
                        "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE_SHA256",
                        sha256_bytes(authority.read_bytes()),
                    )
                )
                stack.enter_context(patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_ENVELOPE", envelope.name))
                stack.enter_context(
                    patch(
                        "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_ENVELOPE_SHA256",
                        envelope_sha256 or sha256_bytes(envelope.read_bytes()),
                    )
                )
                return validate_unslop_authority(self.root)

        with self.assertRaisesRegex(DrainError, "exactly the selected authoritative human text"):
            validate_with_configuration(local_only)
        forged = self.root / "forged-envelope.md"
        forged.write_text(
            f'<human_instruction authoritative="true" source="manager_mail/other.txt:3-3">\n{UNSLOP_AUTHORITY_TEXT}\n</human_instruction>\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DrainError, "exactly the selected authoritative human text"):
            validate_with_configuration(forged)
        exact = self.root / "exact-envelope.md"
        exact.write_text(
            f'<human_instruction authoritative="true" source="manager_mail/unslop-authority.txt:3-3">\n{UNSLOP_AUTHORITY_TEXT}\n</human_instruction>\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(DrainError, "does not match its digest"):
            validate_with_configuration(exact, "0" * 64)

    def test_unslop_bind_and_reconcile_are_exact_no_mail_no_pane_action(self) -> None:
        task_path = self.root / UNSLOP_TASK
        todo_path = self.root / "TODO.md"
        human_authority = self.authority.parent / "unslop-authority.txt"
        human_authority_envelope = self.root / "unslop-authority-envelope.md"
        shared_manager = self.root / "shared-manager.md"
        custody_manager = self.root / "custody-manager.md"
        task_path.write_text(unslop_task(), encoding="utf-8")
        human_authority.write_text(f"Subject: Authorize exact Unslop reconciliation\n\n{UNSLOP_AUTHORITY_TEXT}\n", encoding="utf-8")
        human_authority.chmod(0o600)
        human_authority.parent.chmod(0o700)
        human_authority_envelope.write_text(
            f'<human_instruction authoritative="true" source="manager_mail/unslop-authority.txt:3-3">\n{UNSLOP_AUTHORITY_TEXT}\n</human_instruction>\n',
            encoding="utf-8",
        )
        todo_path.write_text("current:\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")
        shared_manager.write_text(active_manager_task(UNSLOP_TARGET, "wl:0"), encoding="utf-8")
        custody_manager.write_text(active_manager_task("wl:3", "upper:1"), encoding="utf-8")
        for arguments in (
            ["init", "-q"],
            ["config", "user.email", "test@example.com"],
            ["config", "user.name", "Test"],
            ["add", "."],
            ["commit", "-qm", "fixture"],
        ):
            _ = subprocess.run(["git", "-C", str(self.root), *arguments], check=True)
        result_root = self.private / "result-repo"
        remote_root = self.private / "result-remote.git"
        _ = subprocess.run(["git", "init", "--bare", "-q", str(remote_root)], check=True)
        result_root.mkdir()
        (result_root / "result.txt").write_text("complete\n", encoding="utf-8")
        for arguments in (
            ["init", "-q"],
            ["config", "user.email", "test@example.com"],
            ["config", "user.name", "Test"],
            ["add", "."],
            ["commit", "-qm", "result"],
        ):
            _ = subprocess.run(["git", "-C", str(result_root), *arguments], check=True)
        result_commit = subprocess.run(
            ["git", "-C", str(result_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        _ = subprocess.run(
            ["git", "-C", str(result_root), "remote", "add", "origin", str(remote_root)],
            check=True,
        )
        _ = subprocess.run(
            ["git", "-C", str(result_root), "push", "-q", "origin", f"{result_commit}:refs/heads/macos"],
            check=True,
        )
        with (
            patch("omo_manager.omo_namespace_drain.RAW_ANCESTRY_MAX_COMMITS", 0),
            self.assertRaisesRegex(DrainError, "ancestry exceeds its commit bound"),
        ):
            raw_commit_contains_ancestor(result_root, result_commit, result_commit)
        expected_pane = shared_snapshot()
        review = self.private / "unused-review-unslop.json"
        consumed = self.private / "unused-consumed-unslop.json"
        binding_path = self.private / "unslop-binding.json"
        audit = self.private / "unslop-audit.json"
        task_sha256 = sha256_bytes(task_path.read_bytes())
        graph_cas_path = self.private / "unslop-graph-cas.json"
        with patch("omo_manager.omo_namespace_drain.UNSLOP_TASK_SHA256", task_sha256):
            task_replacement = unslop_task_replacement(self.root, task_path.read_bytes())
            graph_snapshot = capture_active_graph(self.root, enumerate_task_record_paths(self.root))
            graph_cas = unslop_graph_cas_value(graph_snapshot, task_path.read_bytes(), task_replacement, self.root)
        graph_cas_path.write_bytes(canonical_json(graph_cas))
        graph_cas_path.chmod(0o600)
        graph_cas_sha256 = sha256_bytes(graph_cas_path.read_bytes())
        with (
            patch("omo_manager.omo_namespace_drain.UNSLOP_TASK_SHA256", task_sha256),
            patch("omo_manager.omo_namespace_drain.UNSLOP_RESULT_ROOT", result_root),
            patch("omo_manager.omo_namespace_drain.UNSLOP_RESULT_COMMIT", result_commit),
            patch("omo_manager.omo_namespace_drain.UNSLOP_RESULT_REMOTE_URL", str(remote_root)),
            patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE", "manager_mail/unslop-authority.txt"),
            patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_LINES", (3, 3)),
            patch(
                "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE_SHA256",
                sha256_bytes(human_authority.read_bytes()),
            ),
            patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_ENVELOPE", "unslop-authority-envelope.md"),
            patch(
                "omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_ENVELOPE_SHA256",
                sha256_bytes(human_authority_envelope.read_bytes()),
            ),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%99"),
            patch("omo_manager.omo_namespace_drain.validate_code_review", return_value={"reviewer": "cedit:28"}),
            patch("omo_manager.omo_namespace_drain.shared_pane_snapshot", return_value=expected_pane) as pane_snapshot,
            patch("omo_manager.omo_namespace_drain.build_completion_email") as completion_email,
            patch("omo_manager.omo_namespace_drain.guarded_remove_shell") as remove_shell,
            patch("omo_manager.omo_namespace_drain.guarded_codex_stop") as stop_codex,
            patch("omo_manager.omo_namespace_drain.tmux_bytes") as pane_input,
        ):
            result = bind_unslop_done(
                self.root,
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                graph_cas_path,
                graph_cas_sha256,
                binding_path,
                "executor:1",
            )

            def rebound_binding(suffix: str) -> tuple[Path, dict[str, object]]:
                rebound_graph_path = self.private / f"unslop-graph-cas-{suffix}.json"
                rebound_snapshot = capture_active_graph(self.root, enumerate_task_record_paths(self.root))
                rebound_value = unslop_graph_cas_value(
                    rebound_snapshot,
                    task_path.read_bytes(),
                    unslop_task_replacement(self.root, task_path.read_bytes()),
                    self.root,
                )
                rebound_graph_path.write_bytes(canonical_json(rebound_value))
                rebound_graph_path.chmod(0o600)
                rebound_path = self.private / f"unslop-binding-{suffix}.json"
                rebound = bind_unslop_done(
                    self.root,
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    rebound_graph_path,
                    sha256_bytes(rebound_graph_path.read_bytes()),
                    rebound_path,
                    "executor:1",
                )
                return rebound_path, rebound
            with (
                patch("omo_manager.omo_namespace_drain.UNSLOP_AUTHORITY_SOURCE", None),
                self.assertRaisesRegex(DrainError, "not configured from an exact Human-instruction injection"),
            ):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "shipped-config-audit.json",
                    "executor:1",
                )
            _ = subprocess.run(
                ["git", "--git-dir", str(remote_root), "update-ref", "-d", "refs/heads/macos"],
                check=True,
            )
            with self.assertRaisesRegex(DrainError, "repository state|capture repository state"):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "unpushed-audit.json",
                    "executor:1",
                )
            _ = subprocess.run(
                ["git", "--git-dir", str(remote_root), "update-ref", "refs/heads/macos", result_commit],
                check=True,
            )
            _ = subprocess.run(
                ["git", "-C", str(result_root), "update-ref", f"refs/replace/{result_commit}", result_commit],
                check=True,
            )
            with self.assertRaisesRegex(DrainError, "replacement refs"):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "replace-ref-audit.json",
                    "executor:1",
                )
            _ = subprocess.run(
                ["git", "-C", str(result_root), "update-ref", "-d", f"refs/replace/{result_commit}"],
                check=True,
            )
            grafts = result_root / ".git" / "info" / "grafts"
            grafts.write_text(f"{result_commit}\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "legacy graft"):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "graft-audit.json",
                    "executor:1",
                )
            grafts.unlink()
            real_ancestry = raw_commit_contains_ancestor

            def ancestry_with_graft_injection(root: Path, tip: str, ancestor: str) -> None:
                grafts.write_text(f"{result_commit}\n", encoding="utf-8")
                real_ancestry(root, tip, ancestor)

            with (
                patch(
                    "omo_manager.omo_namespace_drain.raw_commit_contains_ancestor",
                    side_effect=ancestry_with_graft_injection,
                ),
                self.assertRaisesRegex(DrainError, "legacy graft"),
            ):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "mid-proof-graft-audit.json",
                    "executor:1",
                )
            grafts.unlink()
            original_task = task_path.read_bytes()
            task_path.write_bytes(original_task + b"drift\n")
            with self.assertRaisesRegex(DrainError, "task or TODO bytes drifted"):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "task-drift-audit.json",
                    "executor:1",
                )
            task_path.write_bytes(original_task)
            original_todo = todo_path.read_bytes()
            todo_path.write_bytes(original_todo + b"drift\n")
            with self.assertRaisesRegex(DrainError, "task or TODO bytes drifted"):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "todo-drift-audit.json",
                    "executor:1",
                )
            todo_path.write_bytes(original_todo)
            foreign = self.root / "foreign-drift.txt"
            foreign.write_text("drift\n", encoding="utf-8")
            with self.assertRaisesRegex(DrainError, "repository or index state drifted"):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "foreign-drift-audit.json",
                    "executor:1",
                )
            foreign.unlink()
            with (
                patch(
                    "omo_manager.omo_namespace_drain.shared_pane_snapshot",
                    return_value=replace(expected_pane, pane_pid=806865, pane_start_ticks=12346),
                ),
                self.assertRaisesRegex(DrainError, "shared pane identity drifted"),
            ):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "pane-drift-audit.json",
                    "executor:1",
                )
            bound_authority = json.loads(binding_path.read_text(encoding="utf-8"))["human_authority"]
            with (
                patch(
                    "omo_manager.omo_namespace_drain.validate_unslop_authority",
                    side_effect=[
                        bound_authority,
                        DrainError("Unslop Human authority drifted during reconciliation"),
                    ],
                ),
                self.assertRaisesRegex(DrainError, "Human authority drifted during reconciliation"),
            ):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "authority-drift-audit.json",
                    "executor:1",
                )
            self.assertEqual(original_task, task_path.read_bytes())
            binding_path, result = rebound_binding("after-authority-rollback")
            bound_result = json.loads(binding_path.read_text(encoding="utf-8"))["result"]
            with (
                patch(
                    "omo_manager.omo_namespace_drain.unslop_result_snapshot",
                    side_effect=[
                        bound_result,
                        {**bound_result, "remote_tip": "0" * 40},
                    ],
                ),
                self.assertRaisesRegex(DrainError, "result repository drifted during reconciliation"),
            ):
                close_unslop_done(
                    self.root,
                    binding_path,
                    str(result["sha256"]),
                    review,
                    "1" * 64,
                    consumed,
                    "2" * 64,
                    self.private / "remote-drift-audit.json",
                    "executor:1",
                )
            self.assertEqual(original_task, task_path.read_bytes())
            binding_path, result = rebound_binding("after-result-rollback")
            close_unslop_done(
                self.root,
                binding_path,
                str(result["sha256"]),
                review,
                "1" * 64,
                consumed,
                "2" * 64,
                audit,
                "executor:1",
            )
        metadata = parse_task_metadata(task_path.read_text(encoding="utf-8"), self.root)
        assert metadata is not None
        self.assertEqual("done", metadata.status)
        self.assertEqual(original_todo, todo_path.read_bytes())
        self.assertIn(UNSLOP_ACCEPTED_EVIDENCE, task_path.read_text(encoding="utf-8"))
        self.assertEqual("untouched", json.loads(audit.read_text())["pane_policy"])
        self.assertGreaterEqual(pane_snapshot.call_count, 3)
        completion_email.assert_not_called()
        remove_shell.assert_not_called()
        stop_codex.assert_not_called()
        pane_input.assert_not_called()

    def test_source1385_replacements_move_one_queue_and_remove_stale_session(self) -> None:
        source = self.root / "mail_compress_1261.md"
        source_data = """---
version: v1.0.0
status: blocked
blocked_on: waiting for transfer
runat: cedit:25
tool: codex
managerat: cedit:27
is_manager: false
session_id: 01a0369c-7895-70f2-ae4b-5f59d920e99a
pending_task_items:
  - preserve first receipt
  - preserve second receipt
---
evidence body
""".encode()
        old_after, successor = source1385_task_replacements(
            self.root,
            source,
            source_data,
            "mail_compress_1386.md",
            "wl:43",
            "wl:44",
        )
        old_metadata = parse_task_metadata(old_after.decode(), self.root)
        successor_metadata = parse_task_metadata(successor.decode(), self.root)
        assert old_metadata is not None and successor_metadata is not None
        self.assertEqual(("done", ()), (old_metadata.status, old_metadata.pending_task_items))
        self.assertEqual(("blocked", "wl:43", "wl:44"), (successor_metadata.status, successor_metadata.runat, successor_metadata.managerat))
        self.assertEqual(("preserve first receipt", "preserve second receipt"), successor_metadata.pending_task_items)
        self.assertEqual("", successor_metadata.session_id)

        todo = b"current:\nmail_compress_1261.md cedit:25\n\nhuman pending:\n\nlow priority:\n\nprevious:\n"
        moved = source1385_todo_replacement(self.root, todo, source, self.root / "mail_compress_1386.md", "wl:43")
        self.assertIn(b"mail_compress_1386.md wl:43", moved)
        self.assertNotIn(b"mail_compress_1261.md cedit:25", moved)

    def test_source1385_eligibility_is_exact_and_session_bound(self) -> None:
        value = {
            "schema": "omo-namespace-drain-source1385-successor-eligibility/v1",
            "accepted": True,
            "accepted_by": "wl:44",
            "successor_target": "wl:43.0",
            "successor_task": "mail_compress_1386.md",
            "successor_manager": "wl:44",
            "successor_manager_task": "manager.md",
            "successor_manager_task_sha256": "1" * 64,
            "source_session_id": "01a0369c-7895-70f2-ae4b-5f59d920e99a",
            "authority_sha256": namespace_drain.SOURCE1385_AUTHORITY_SHA256,
        }
        data = canonical_json(value)
        self.assertEqual(value, validate_source1385_successor_eligibility(data, sha256_bytes(data), "wl:43.0", "mail_compress_1386.md"))
        value["extra"] = "not accepted"
        changed = canonical_json(value)
        with self.assertRaisesRegex(DrainError, "exact accepted binding"):
            validate_source1385_successor_eligibility(changed, sha256_bytes(changed), "wl:43.0", "mail_compress_1386.md")

    def test_source1385_phase_journal_rejects_skipped_transition(self) -> None:
        audit = self.private / "handoff-audit.json"
        with pinned_directory(self.private) as directory:
            with self.assertRaisesRegex(DrainError, "not sequential"):
                record_source1385_phase(directory, audit, "a" * 64, "b" * 64, "c" * 64, "", "pane-removed")
            phase = record_source1385_phase(directory, audit, "a" * 64, "b" * 64, "c" * 64, "", "prepared")
            self.assertEqual("prepared", phase)
            with self.assertRaisesRegex(DrainError, "phase drifted"):
                record_source1385_phase(directory, audit, "a" * 64, "b" * 64, "c" * 64, "", "prepared")

    def test_source1385_handoff_refuses_before_stopping_or_mutating(self) -> None:
        source = self.root / "mail_compress_1261.md"
        source.write_text(
            """---
version: v1.0.0
status: blocked
blocked_on: waiting for transfer
runat: cedit:25
tool: codex
managerat: cedit:27
is_manager: false
pending_task_items:
  - preserve exact queue
---
evidence body
""",
            encoding="utf-8",
        )
        manager = self.root / "manager.md"
        manager.write_text(
            """---
version: v1.0.0
status: long_running
runat: wl:44
tool: codex
managerat: root:0
is_manager: true
pending_task_items: []
---
manager
""",
            encoding="utf-8",
        )
        source_manager = self.root / "source-manager.md"
        source_manager.write_text(
            """---
version: v1.0.0
status: long_running
runat: cedit:27
tool: codex
managerat: root:0
is_manager: true
pending_task_items: []
---
source manager
""",
            encoding="utf-8",
        )
        todo = self.root / "TODO.md"
        todo.write_text(
            "current:\nmail_compress_1261.md cedit:25\nmanager.md wl:44\n\nhuman pending:\n\nlow priority:\n\nprevious:\n",
            encoding="utf-8",
        )
        authority = self.root / "manager_mail" / "85c5dff58359-1386.txt"
        authority.write_text(SOURCE1385_AUTHORITY_TEXT + "\n", encoding="utf-8")
        authority_sha = sha256_bytes(authority.read_bytes())
        eligibility_value = {
            "schema": "omo-namespace-drain-source1385-successor-eligibility/v1",
            "accepted": True,
            "accepted_by": "wl:44",
            "successor_target": "wl:43.0",
            "successor_task": "mail_compress_1386.md",
            "successor_manager": "wl:44",
            "successor_manager_task": "manager.md",
            "successor_manager_task_sha256": sha256_bytes(manager.read_bytes()),
            "source_session_id": "01a0369c-7895-70f2-ae4b-5f59d920e99a",
            "authority_sha256": authority_sha,
        }
        eligibility = self.private / "eligibility.json"
        eligibility.write_bytes(canonical_json(eligibility_value))
        eligibility.chmod(0o600)
        review = self.private / "review.json"
        consumed = self.private / "consumed.json"
        review.write_text("{}", encoding="utf-8")
        consumed.write_text("{}", encoding="utf-8")
        review.chmod(0o600)
        consumed.chmod(0o600)
        binding = self.private / "handoff-binding.json"
        audit = self.private / "handoff-audit.json"
        live = {"value": True}

        def inspect(target: str) -> str:
            return "running" if target == "cedit:25" and live["value"] else "absent"

        def stop(_target: str, _pane: str, _pid: int, _ticks: int, session: str = "") -> None:
            self.assertEqual(eligibility_value["source_session_id"], session)
            live["value"] = False

        def manager_owner(_root: Path, target: str) -> tuple[Path, bytes]:
            selected = source_manager if target == "cedit:27" else manager
            return selected.resolve(), selected.read_bytes()

        def manager_pane(target: str) -> str:
            return "%27" if target == "cedit:27" else "%44"

        with (
            patch("omo_manager.omo_namespace_drain.SOURCE1385_AUTHORITY_SHA256", authority_sha),
            patch("omo_manager.omo_namespace_drain.validate_source1385_code_review", return_value={"reviewer": "review:1"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch("omo_manager.omo_namespace_drain.inspect_target", side_effect=inspect),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%25", 101, 202)),
            patch("omo_manager.omo_namespace_drain.exact_pane_id", side_effect=manager_pane),
            patch("omo_manager.omo_namespace_drain.active_manager_owner", side_effect=manager_owner),
            patch("omo_manager.omo_namespace_drain.repository_dirty_snapshot", return_value={"schema": "test"}),
        ):
            result = bind_source1385_live_worker_handoff(
                self.root,
                review,
                sha256_bytes(review.read_bytes()),
                consumed,
                sha256_bytes(consumed.read_bytes()),
                binding,
                "executor:1",
                sha256_bytes(source.read_bytes()),
                sha256_bytes(todo.read_bytes()),
                "mail_compress_1386.md",
                "wl:43.0",
                eligibility,
                sha256_bytes(eligibility.read_bytes()),
            )
        binding_sha = str(result["sha256"])
        original_source = source.read_bytes()
        original_todo = todo.read_bytes()
        stop_mock = patch("omo_manager.omo_namespace_drain.stop_target", side_effect=stop)
        with (
            patch("omo_manager.omo_namespace_drain.SOURCE1385_AUTHORITY_SHA256", authority_sha),
            patch("omo_manager.omo_namespace_drain.validate_source1385_code_review", return_value={"reviewer": "review:1"}),
            patch("omo_manager.omo_namespace_drain.authenticate_executor", return_value="%9"),
            patch("omo_manager.omo_namespace_drain.inspect_target", side_effect=inspect),
            patch("omo_manager.omo_namespace_drain.target_identity", return_value=("%25", 101, 202)),
            patch("omo_manager.omo_namespace_drain.recovery_pane_presence", side_effect=lambda _target, _pane: "present" if live["value"] else "absent"),
            patch("omo_manager.omo_namespace_drain.process_start_ticks", side_effect=lambda _pid: 202 if live["value"] else None),
            patch("omo_manager.omo_namespace_drain.exact_pane_id", side_effect=manager_pane),
            patch("omo_manager.omo_namespace_drain.active_manager_owner", side_effect=manager_owner),
            patch("omo_manager.omo_namespace_drain.validate_dirty_snapshot", return_value={"schema": "test"}),
            stop_mock as stop_call,
        ):
            with self.assertRaisesRegex(DrainError, re.escape(SOURCE1385_PUBLIC_MUTATION_BLOCKER)):
                close_source1385_live_worker_handoff(
                    self.root,
                    binding,
                    binding_sha,
                    review,
                    sha256_bytes(review.read_bytes()),
                    consumed,
                    sha256_bytes(consumed.read_bytes()),
                    audit,
                    "executor:1",
                )
        stop_call.assert_not_called()
        self.assertTrue(live["value"])
        self.assertEqual(original_source, source.read_bytes())
        self.assertEqual(original_todo, todo.read_bytes())
        self.assertFalse((self.root / "mail_compress_1386.md").exists())
        self.assertFalse(audit.exists())

    def test_dirty_manifest_excludes_only_exact_lifecycle_files(self) -> None:
        task_path = self.root / COMPLETED_SHELL_TASK
        todo_path = self.root / "TODO.md"
        other = self.root / "other.txt"
        task_path.write_text("task\n", encoding="utf-8")
        todo_path.write_text("todo\n", encoding="utf-8")
        other.write_text("one\n", encoding="utf-8")
        _ = subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.com"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        _ = subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "fixture"], check=True)
        other.write_text("dirty\n", encoding="utf-8")
        first = repository_dirty_snapshot(self.root, (task_path, todo_path))
        task_path.write_text("changed\n", encoding="utf-8")
        todo_path.write_text("changed\n", encoding="utf-8")
        self.assertEqual(first, repository_dirty_snapshot(self.root, (task_path, todo_path)))
        other.write_text("foreign drift\n", encoding="utf-8")
        self.assertNotEqual(first, repository_dirty_snapshot(self.root, (task_path, todo_path)))


if __name__ == "__main__":
    unittest.main()
