from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import yaml
from contextlib import nullcontext
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import DEFAULT
from unittest.mock import patch

from omo_manager.omo_agent_status import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_task_status import ACTIVE_TASK_TREE_AUTHORITY_TEXT
from omo_manager.omo_task_status import ACTIVE_TASK_TREE_BLOCKER
from omo_manager.omo_task_status import ACTIVE_TASK_TREE_NO_MAIL_INTENT
from omo_manager.omo_task_status import DONE_REMINDER
from omo_manager.omo_task_status import DoneLiveCloseAudit
from omo_manager.omo_task_status import active_task_tree_todo_replacement
from omo_manager.omo_task_status import ensure_repository_closure_custody
from omo_manager.omo_task_status import close_active_task_tree_no_mail
from omo_manager.omo_task_status import close_done_live_no_mail
from omo_manager.omo_task_status import close_retired_done
from omo_manager.omo_task_status import close_missing_target
from omo_manager.omo_task_status import cancel_shared_target_done
from omo_manager.omo_task_status import normalize_retired_todo
from omo_manager.omo_task_status import normalize_low_priority_current
from omo_manager.omo_task_status import park_audit_record
from omo_manager.omo_task_status import park_target_pane_id
from omo_manager.omo_task_status import park_unlinked
from omo_manager.omo_task_status import reattest_park_unlinked
from omo_manager.omo_task_status import finish_done_transaction
from omo_manager.omo_task_status import finish_shared_target_done
from omo_manager.omo_task_status import finish_private_audit
from omo_manager.omo_task_status import StopArgs
from omo_manager.omo_task_status import parse_args
from omo_manager.omo_task_status import reconcile_blocked_index
from omo_manager.omo_task_status import reconcile_done_index
from omo_manager.omo_task_status import reconcile_running_index
from omo_manager.omo_task_status import reconcile_long_running_human_index
from omo_manager.omo_task_status import reconcile_missing_target
from omo_manager.omo_task_status import read_park_authority_envelope
from omo_manager.omo_task_status import render_done_live_close_audit
from omo_manager.omo_task_status import replace_if_unchanged_locked
from omo_manager.omo_task_status import replace_private_audit
from omo_manager.omo_task_status import reserve_private_audit
from omo_manager.omo_task_status import restore_terminal_target
from omo_manager.omo_task_status import run
from omo_manager.omo_task_status import stop_done_agent
from omo_manager.omo_task_status import tracked_dirty_state
from omo_manager.omo_task_status import update_frontmatter_status
from omo_manager.omo_task_status import validate_manager_consumed_report
from omo_manager.omo_task_status import Args as StatusArgs
from omo_manager.omo_codex_stop import ExitedCodexShell
from omo_manager.omo_codex_stop import done_live_close_started_path
from omo_manager.omo_codex_stop import write_bound_close_proof
from omo_manager.omo_codex_stop import write_done_live_close_started
from omo_manager.omo_codex_stop import promote_done_live_close_started
from omo_manager.omo_codex_stop import close_note
from omo_manager.omo_task_metadata import frontmatter_parts
from omo_manager.omo_blocking import ENABLE_FILE, load_yaml_mapping, render_task, split_task_text, sync_generated_blocker
from omo_manager.tests.test_task_metadata_v2 import v2_task


def task_frontmatter(
    *,
    status: str = "running",
    blocked_on: str = "",
    pending_items: tuple[str, ...] = (),
    runat: str = "wl:2",
    managerat: str = "wl:1",
    is_manager: bool = False,
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
    ]
    if blocked_on:
        lines.append(f"blocked_on: {blocked_on}")
    lines.extend(
        [
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
        ]
    )
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.append("---")
    return "\n".join(lines) + "\n"


def write_active_task_tree_fixture(root: Path) -> tuple[Path, Path, Path, StatusArgs, str, str, str]:
    task = root / "active_task_tree.md"
    protected = root / "202608" / "mail_report_policy.md"
    todo = root / "TODO.md"
    authority = root / "manager_mail" / "85c5dff58359-1298.txt"
    protected.parent.mkdir()
    authority.parent.mkdir()
    task_text = task_frontmatter(status="blocked", blocked_on=ACTIVE_TASK_TREE_BLOCKER, runat="agent_managers:5", managerat="hwl:3") + "completed evidence stays\n"
    protected_text = task_frontmatter(status="blocked", blocked_on="human", runat="agent_managers:5", managerat="agent_managers:7") + "active protected task stays\n"
    todo_text = "current:\nactive_task_tree.md agent_managers:5\n\nlow priority:\n\nhuman pending:\n\nprevious:\n"
    authority_text = f"Subject: Source 1298\n\n{ACTIVE_TASK_TREE_AUTHORITY_TEXT}\n"
    task.write_text(task_text, encoding="utf-8")
    protected.write_text(protected_text, encoding="utf-8")
    todo.write_text(todo_text, encoding="utf-8")
    authority.write_text(authority_text, encoding="utf-8")
    args = StatusArgs(
        root,
        Path("active_task_tree.md"),
        "done",
        "",
        close_active_task_tree_no_mail=True,
        shared_target="agent_managers:5",
        protected_shared_task=Path("202608/mail_report_policy.md"),
        protected_shared_sha256=hashlib.sha256(protected_text.encode()).hexdigest(),
        expected_task_sha256=hashlib.sha256(task_text.encode()).hexdigest(),
        expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
        expected_pane_id="%3387",
        authority_file=Path("manager_mail/85c5dff58359-1298.txt"),
        authority_lines=(3, 4),
        authority_sha256=hashlib.sha256(authority_text.encode()).hexdigest(),
        no_mail_intent=ACTIVE_TASK_TREE_NO_MAIL_INTENT,
    )
    return task, protected, todo, args, task_text, protected_text, todo_text


class TaskStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        delivered = patch("omo_manager.omo_task_status.require_owner_completion", return_value=True)
        _ = delivered.start()
        self.addCleanup(delivered.stop)

    AUTHORITY_TEXT = (
        "Subject: stop paused roles\n\n"
        "As I said previously, the human has halted all work regarding VL. Agents\n"
        "can make decisions by themselves. But the human would not do anything about\n"
        "them. If an agent is paused, take it down and consider it closed and\n"
        "instead put the pending item under the to do file without a linked agent.\n"
    )
    RECONCILE_AUTHORITY_TEXT = (
        "Subject: correct missing task records\n\n"
        "I don't see any task I am aware of that is not tracked, so just correct "
        "the task records as opposed to reinstating the agents.\n"
    )
    CLOSE_MISSING_AUTHORITY_TEXT = (
        "Subject: close missing records\n\n"
        "As for the sessions without IDs, do whatever you need to do to make those\n"
        "problems go away. If they don't provide the session ID, force close them. So maybe just close them.\n\n"
        "The seven records with missing tmux targets were ordered closed.\n"
    )

    def write_close_missing_case(
        self,
        root: Path,
        *,
        section: str = "human pending",
        targetful: bool = True,
        blocker: str = "direct human halt",
        is_manager: bool = False,
    ) -> tuple[Path, str, Path, str, StatusArgs]:
        authority_dir = root / "manager_mail"
        authority_dir.mkdir(mode=0o700)
        authority = authority_dir / "close.txt"
        authority.write_text(self.CLOSE_MISSING_AUTHORITY_TEXT, encoding="utf-8")
        authority.chmod(0o600)
        excerpt = "".join(self.CLOSE_MISSING_AUTHORITY_TEXT.splitlines(keepends=True)[2:6])
        envelope = root / "request_task.md"
        envelope_text = (
            '<human_instruction authoritative="true" source="manager_mail/close.txt:3-6">\n'
            f"{excerpt}</human_instruction>\n"
        )
        envelope.write_text(envelope_text, encoding="utf-8")
        text = task_frontmatter(
            status="blocked",
            blocked_on=blocker,
            pending_items=("preserve first", "preserve second"),
            runat="vl:8",
            is_manager=is_manager,
        ) + "existing evidence\n"
        task = root / "missing.md"
        task.write_text(text, encoding="utf-8")
        row = "missing.md vl:8" if targetful else "missing.md"
        todo_text = (
            f"current:\n{row if section == 'current' else ''}\n\n"
            f"low priority:\n{row if section == 'low priority' else ''}\n\n"
            f"human pending:\n{row if section == 'human pending' else ''}\n\n"
            f"previous:\n{row if section == 'previous' else ''}\n"
        )
        todo = root / "TODO.md"
        todo.write_text(todo_text, encoding="utf-8")
        audit_dir = root / "audit"
        audit_dir.mkdir(mode=0o700)
        args = StatusArgs(
            root,
            Path("missing.md"),
            "",
            "",
            close_missing_target=True,
            missing_target="vl:8",
            expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
            authority_file=Path("manager_mail/close.txt"),
            authority_lines=(3, 6),
            authority_sha256=hashlib.sha256(self.CLOSE_MISSING_AUTHORITY_TEXT.encode()).hexdigest(),
            authority_envelope=Path("request_task.md"),
            authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
            audit_output=(audit_dir / "close.yaml").resolve(),
        )
        return task, text, todo, todo_text, args

    def test_close_missing_target_handles_all_canonical_sections_and_record_shapes(self) -> None:
        shapes = (
            ("current", True, "human", False),
            ("human pending", False, "direct human halt", True),
            ("low priority", True, "token quota", False),
            ("low priority", False, "reviewed lifecycle helper limitation", True),
            ("previous", True, "direct human halt", True),
        )
        for section, targetful, blocker, is_manager in shapes:
            with self.subTest(section=section, targetful=targetful, blocker=blocker, is_manager=is_manager), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, _todo_text, args = self.write_close_missing_case(
                    root,
                    section=section,
                    targetful=targetful,
                    blocker=blocker,
                    is_manager=is_manager,
                )
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "", "")) as inspect_target,
                    patch("omo_manager.omo_task_status.stop", side_effect=AssertionError("tmux mutation")),
                ):
                    close_missing_target(args, task, text, task.stat())
                self.assertEqual(3, inspect_target.call_count)
                metadata = parse_task_metadata(task.read_text(), root)
                assert metadata is not None
                self.assertEqual("done", metadata.status)
                self.assertEqual((), metadata.pending_task_items)
                self.assertIn("prior queue preserved in owner-private audit", task.read_text())
                previous = todo.read_text().partition("previous:\n")[2]
                self.assertIn("missing.md\n", previous)
                self.assertNotIn("missing.md vl:8", todo.read_text())
                audit = yaml.safe_load(args.audit_output.read_text())
                self.assertEqual("complete", audit["state"])
                self.assertEqual(["preserve first", "preserve second"], audit["pending_task_items"])
                self.assertEqual("token quota" if blocker == "token quota" else blocker, audit["blocked_on"])
                self.assertEqual("request_task.md", audit["authority_envelope"])

    def test_close_missing_target_rejects_live_unprovable_or_rebound_target_without_mutation(self) -> None:
        resolutions = (("%42",), (None,), ("", "%42"), ("", "", "%42"))
        for resolution in resolutions:
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_close_missing_case(root)
                with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=resolution):
                    with self.assertRaisesRegex(TaskFrontmatterError, "live|reappeared"):
                        close_missing_target(args, task, text, task.stat())
                self.assertEqual(text, task.read_text())
                self.assertEqual(todo_text, todo.read_text())

    def test_close_missing_target_rejects_drift_malformed_authority_and_ambiguous_todo(self) -> None:
        for case in (
            "task_digest", "todo_digest", "authority", "negated_authority", "unrelated_closure",
            "question_authority", "conditional_authority", "temporal_authority", "envelope", "duplicate",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_close_missing_case(root)
                if case == "task_digest":
                    args = replace(args, expected_task_sha256="0" * 64)
                elif case == "todo_digest":
                    args = replace(args, expected_todo_sha256="0" * 64)
                elif case == "authority":
                    authority = root / "manager_mail/close.txt"
                    malformed = self.CLOSE_MISSING_AUTHORITY_TEXT.replace("So maybe just close them.", "Review them.").replace("ordered closed", "need review")
                    authority.write_text(malformed)
                    authority.chmod(0o600)
                    args = replace(args, authority_sha256=hashlib.sha256(malformed.encode()).hexdigest())
                elif case == "negated_authority":
                    authority = root / "manager_mail/close.txt"
                    negated = self.CLOSE_MISSING_AUTHORITY_TEXT.replace(
                        "So maybe just close them.",
                        "So maybe just close them. Do not close the missing records.",
                    )
                    authority.write_text(negated)
                    authority.chmod(0o600)
                    envelope = root / "request_task.md"
                    changed_envelope = envelope.read_text().replace(
                        "So maybe just close them.",
                        "So maybe just close them. Do not close the missing records.",
                    )
                    envelope.write_text(changed_envelope)
                    args = replace(
                        args,
                        authority_sha256=hashlib.sha256(negated.encode()).hexdigest(),
                        authority_envelope_sha256=hashlib.sha256(changed_envelope.encode()).hexdigest(),
                    )
                elif case == "unrelated_closure":
                    authority = root / "manager_mail/close.txt"
                    unrelated = "Subject: decision\n\nMissing-target records require review; unrelated records were ordered closed.\n"
                    authority.write_text(unrelated)
                    authority.chmod(0o600)
                    excerpt = unrelated.splitlines(keepends=True)[2]
                    envelope = root / "request_task.md"
                    envelope_text = (
                        '<human_instruction authoritative="true" source="manager_mail/close.txt:3-3">\n'
                        f"{excerpt}</human_instruction>\n"
                    )
                    envelope.write_text(envelope_text)
                    args = replace(
                        args,
                        authority_lines=(3, 3),
                        authority_sha256=hashlib.sha256(unrelated.encode()).hexdigest(),
                        authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
                    )
                elif case in {"question_authority", "conditional_authority", "temporal_authority"}:
                    authority = root / "manager_mail/close.txt"
                    if case == "question_authority":
                        decision = "Are records with missing tmux targets ordered closed?"
                    elif case == "conditional_authority":
                        decision = "If the owner agrees, missing-target records can be closed."
                    else:
                        decision = "Close missing-target records after the owner approves."
                    uncertain = f"Subject: decision\n\n{decision}\n"
                    authority.write_text(uncertain)
                    authority.chmod(0o600)
                    excerpt = uncertain.splitlines(keepends=True)[2]
                    envelope = root / "request_task.md"
                    envelope_text = (
                        '<human_instruction authoritative="true" source="manager_mail/close.txt:3-3">\n'
                        f"{excerpt}</human_instruction>\n"
                    )
                    envelope.write_text(envelope_text)
                    args = replace(
                        args,
                        authority_lines=(3, 3),
                        authority_sha256=hashlib.sha256(uncertain.encode()).hexdigest(),
                        authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
                    )
                elif case == "envelope":
                    envelope = root / "request_task.md"
                    changed = envelope.read_text().replace("ordered closed", "need review")
                    envelope.write_text(changed)
                    args = replace(args, authority_envelope_sha256=hashlib.sha256(changed.encode()).hexdigest())
                else:
                    changed = todo_text.replace("previous:\n", "previous:\nmissing.md vl:8\n")
                    todo.write_text(changed)
                    args = replace(args, expected_todo_sha256=hashlib.sha256(changed.encode()).hexdigest())
                with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                    with self.assertRaises(TaskFrontmatterError):
                        close_missing_target(args, task, task.read_text(), task.stat())
                self.assertEqual(text, task.read_text())

    def test_close_missing_target_rolls_back_todo_when_task_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_close_missing_case(root, section="low priority")
            real_replace = replace_if_unchanged_locked

            def fail_task(path: Path, updated: str, before: os.stat_result) -> None:
                if path == task:
                    raise TaskFrontmatterError("injected task failure")
                real_replace(path, updated, before)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task),
            ):
                with self.assertRaisesRegex(TaskFrontmatterError, "injected task failure"):
                    close_missing_target(args, task, text, task.stat())
            self.assertEqual(text, task.read_text())
            self.assertEqual(todo_text, todo.read_text())

    def test_close_missing_target_recovers_after_todo_commit_before_task_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, _todo_text, args = self.write_close_missing_case(root, section="low priority")
            real_replace = replace_if_unchanged_locked

            def interrupt_task(path: Path, updated: str, before: os.stat_result) -> None:
                if path == task:
                    raise KeyboardInterrupt
                real_replace(path, updated, before)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=interrupt_task),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    close_missing_target(args, task, text, task.stat())
            self.assertEqual(text, task.read_text())
            self.assertIn("previous:\nmissing.md\n", todo.read_text())
            self.assertEqual("prepared-or-committed", yaml.safe_load(args.audit_output.read_text())["state"])

            with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                close_missing_target(args, task, task.read_text(), task.stat())
            metadata = parse_task_metadata(task.read_text(), root)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual((), metadata.pending_task_items)
            self.assertEqual("complete", yaml.safe_load(args.audit_output.read_text())["state"])

    def test_close_missing_target_rejects_post_preparation_task_drift_fresh_and_recovery(self) -> None:
        for recovery in (False, True):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_close_missing_case(root, section="low priority")
                if recovery:
                    real_replace = replace_if_unchanged_locked

                    def interrupt_task(path: Path, updated: str, before: os.stat_result) -> None:
                        if path == task:
                            raise KeyboardInterrupt
                        real_replace(path, updated, before)

                    with (
                        patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
                        patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=interrupt_task),
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            close_missing_target(args, task, text, task.stat())

                calls = 0
                drifted = text + "concurrent drift\n"

                def drift_before_task_commit(_target: str) -> str:
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        task.write_text(drifted)
                    return ""

                with patch(
                    "omo_manager.omo_task_status.park_target_pane_id",
                    side_effect=drift_before_task_commit,
                ):
                    with self.assertRaisesRegex(TaskFrontmatterError, "changed"):
                        close_missing_target(args, task, task.read_text(), task.stat())
                self.assertEqual(drifted, task.read_text())
                if recovery:
                    self.assertIn("previous:\nmissing.md\n", todo.read_text())
                else:
                    self.assertEqual(todo_text, todo.read_text())
                self.assertEqual("prepared-or-committed", yaml.safe_load(args.audit_output.read_text())["state"])

    def test_close_missing_target_detects_envelope_drift_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_close_missing_case(root)
            envelope = root / "request_task.md"
            original = envelope.read_text()
            real_read = read_park_authority_envelope
            reads = 0

            def drift_after_first_read(call_args: StatusArgs, excerpt: str, locator: str) -> str:
                nonlocal reads
                reads += 1
                result = real_read(call_args, excerpt, locator)
                if reads == 1:
                    envelope.write_text(original + "drift\n")
                return result

            with (
                patch("omo_manager.omo_task_status.read_park_authority_envelope", side_effect=drift_after_first_read),
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
            ):
                with self.assertRaisesRegex(TaskFrontmatterError, "authority envelope"):
                    close_missing_target(args, task, text, task.stat())
            self.assertEqual(text, task.read_text())
            self.assertEqual(todo_text, todo.read_text())
            self.assertFalse(args.audit_output.exists())

    def test_close_missing_target_only_reads_tmux_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, _todo, _todo_text, args = self.write_close_missing_case(root)
            empty_inventory = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with (
                patch("omo_manager.omo_task_status.tmux", return_value=empty_inventory) as tmux_call,
                patch("omo_manager.omo_task_status.stop", side_effect=AssertionError("pane mutation")),
                patch("omo_manager.omo_task_status.record_close", side_effect=AssertionError("close mutation")),
            ):
                close_missing_target(args, task, text, task.stat())
            self.assertEqual(3, tmux_call.call_count)
            expected = [
                "list-panes",
                "-a",
                "-F",
                "#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_active}\t#{pane_id}",
            ]
            self.assertTrue(all(call.args == (expected,) for call in tmux_call.call_args_list))

    def test_close_missing_target_treats_prepared_audit_as_committed_when_finalization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, _todo_text, args = self.write_close_missing_case(root)
            real_replace_audit = replace_private_audit

            def fail_complete(path: Path, expected: str, updated: str) -> None:
                if yaml.safe_load(updated)["state"] == "complete":
                    raise TaskFrontmatterError("injected audit finalization failure")
                real_replace_audit(path, expected, updated)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.replace_private_audit", side_effect=fail_complete),
            ):
                close_missing_target(args, task, text, task.stat())
            metadata = parse_task_metadata(task.read_text(), root)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertIn("previous:\nmissing.md\n", todo.read_text())
            self.assertEqual("prepared-or-committed", yaml.safe_load(args.audit_output.read_text())["state"])

            with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                close_missing_target(args, task, task.read_text(), task.stat())
            self.assertEqual("complete", yaml.safe_load(args.audit_output.read_text())["state"])

    def test_close_missing_target_accepts_semantic_direct_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, _todo, _todo_text, args = self.write_close_missing_case(root)
            authority = root / "manager_mail/close.txt"
            semantic = "Subject: decision\n\nClose the seven missing-target records.\n"
            authority.write_text(semantic)
            authority.chmod(0o600)
            excerpt = semantic.splitlines(keepends=True)[2]
            envelope = root / "request_task.md"
            envelope_text = (
                '<human_instruction authoritative="true" source="manager_mail/close.txt:3-3">\n'
                f"{excerpt}</human_instruction>\n"
            )
            envelope.write_text(envelope_text)
            args = replace(
                args,
                authority_lines=(3, 3),
                authority_sha256=hashlib.sha256(semantic.encode()).hexdigest(),
                authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
            )
            with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                close_missing_target(args, task, text, task.stat())
            self.assertEqual("done", parse_task_metadata(task.read_text(), root).status)

    def test_close_missing_target_accepts_crlf_authority_with_exact_digest_and_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, _todo, _todo_text, args = self.write_close_missing_case(root)
            authority = root / "manager_mail/close.txt"
            crlf = authority.read_text().replace("\n", "\r\n")
            authority.write_bytes(crlf.encode())
            authority.chmod(0o600)
            args = replace(args, authority_sha256=hashlib.sha256(crlf.encode()).hexdigest())
            with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                close_missing_target(args, task, text, task.stat())
            metadata = parse_task_metadata(task.read_text(), root)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual("complete", yaml.safe_load(args.audit_output.read_text())["state"])

    def test_close_missing_target_cli_requires_exact_direct_inputs(self) -> None:
        args = parse_args(
            [
                "--root",
                "/tmp/root",
                "--close-missing-target",
                "--missing-target",
                "vl:8",
                "--expected-task-sha256",
                "1" * 64,
                "--expected-todo-sha256",
                "2" * 64,
                "--authority-file",
                "manager_mail/request.txt",
                "--authority-lines",
                "3-4",
                "--authority-sha256",
                "3" * 64,
                "--authority-envelope",
                "task.md",
                "--authority-envelope-sha256",
                "4" * 64,
                "--audit-output",
                "/tmp/audit.yaml",
                "missing.md",
            ]
        )
        self.assertTrue(args.close_missing_target)
        self.assertEqual("vl:8", args.missing_target)
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--root", "/tmp/root", "--close-missing-target", "--missing-target", "vl:8",
                    "--expected-task-sha256", "1" * 64, "--expected-todo-sha256", "2" * 64,
                    "--expected-receipt-sha256", "5" * 64,
                    "--authority-file", "manager_mail/request.txt", "--authority-lines", "3-4",
                    "--authority-sha256", "3" * 64, "--authority-envelope", "task.md",
                    "--authority-envelope-sha256", "4" * 64, "--audit-output", "/tmp/audit.yaml",
                    "missing.md",
                ]
            )
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--close-missing-target",
                    "--missing-target",
                    "vl:8",
                    "--expected-task-sha256",
                    "1" * 64,
                    "--expected-todo-sha256",
                    "2" * 64,
                    "--authority-file",
                    "manager_mail/request.txt",
                    "--authority-lines",
                    "3-4",
                    "--authority-sha256",
                    "3" * 64,
                    "--audit-output",
                    "/tmp/audit.yaml",
                    "missing.md",
                ]
            )

    def write_missing_target_case(self, root: Path, *, runat: str = "vl:8", is_manager: bool = False) -> tuple[Path, str, Path, str, StatusArgs]:
        authority_dir = root / "manager_mail"
        authority_dir.mkdir(mode=0o700)
        authority = authority_dir / "request.txt"
        authority.write_text(self.RECONCILE_AUTHORITY_TEXT, encoding="utf-8")
        authority.chmod(0o600)
        excerpt = self.RECONCILE_AUTHORITY_TEXT.splitlines(keepends=True)[2]
        envelope = root / "request_task.md"
        envelope_text = (
            '<human_instruction authoritative="true" source="manager_mail/request.txt:3-3">\n'
            f"{excerpt}</human_instruction>\n"
        )
        envelope.write_text(envelope_text, encoding="utf-8")
        text = task_frontmatter(
            status="blocked",
            blocked_on="direct human shutdown",
            pending_items=("preserve work",),
            runat=runat,
            is_manager=is_manager,
        ) + "existing evidence\n"
        task = root / "missing.md"
        task.write_text(text, encoding="utf-8")
        todo_text = f"current:\n\nlow priority:\nslow.md vl:7\n\nhuman pending:\nmissing.md {runat}\n\nprevious:\n"
        todo = root / "TODO.md"
        todo.write_text(todo_text, encoding="utf-8")
        args = StatusArgs(
            root,
            Path("missing.md"),
            "",
            "",
            reconcile_missing_target=True,
            missing_target=runat,
            expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
            authority_file=Path("manager_mail/request.txt"),
            authority_lines=(3, 3),
            authority_sha256=hashlib.sha256(self.RECONCILE_AUTHORITY_TEXT.encode()).hexdigest(),
            authority_envelope=Path("request_task.md"),
            authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
        )
        return task, text, todo, todo_text, args

    def test_reconcile_missing_target_corrects_worker_manager_and_human_records_without_tmux_mutation(self) -> None:
        for target, is_manager in (("vl:8", False), ("vl:8", True), ("hvl:8", False)):
            with self.subTest(target=target, is_manager=is_manager), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, _text, todo, _todo_text, args = self.write_missing_target_case(root, runat=target, is_manager=is_manager)
                with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")) as inspect_target:
                    reconcile_missing_target(args, task, task.read_text(), task.stat())
                self.assertEqual(2, inspect_target.call_count)
                self.assertIn("runat: retired", task.read_text())
                self.assertIn(f"historical tmux target retired: {target}", task.read_text())
                self.assertIn("low priority:\nmissing.md\n", todo.read_text())
                self.assertNotIn(f"missing.md {target}", todo.read_text())

    def test_reconcile_missing_target_rejects_live_or_unprovable_target_without_mutation(self) -> None:
        for resolution in ("%42", None):
            with self.subTest(resolution=resolution), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_missing_target_case(root)
                with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=resolution):
                    with self.assertRaisesRegex(TaskFrontmatterError, "live or tmux could not prove"):
                        reconcile_missing_target(args, task, text, task.stat())
                self.assertEqual(text, task.read_text())
                self.assertEqual(todo_text, todo.read_text())

    def test_reconcile_missing_target_rejects_nonhuman_blocker_or_wrong_todo_custody(self) -> None:
        for case in ("nonhuman", "hyphenated_nonhuman", "current"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_missing_target_case(root)
                if case in {"nonhuman", "hyphenated_nonhuman"}:
                    blocker = "non-human dependency" if case == "hyphenated_nonhuman" else "build dependency"
                    changed = text.replace("blocked_on: direct human shutdown", f"blocked_on: {blocker}")
                    task.write_text(changed, encoding="utf-8")
                    args = replace(args, expected_task_sha256=hashlib.sha256(changed.encode()).hexdigest())
                else:
                    changed_todo = todo_text.replace("human pending:\nmissing.md vl:8", "human pending:\n\ncurrent:\nmissing.md vl:8")
                    todo.write_text(changed_todo, encoding="utf-8")
                    args = replace(args, expected_todo_sha256=hashlib.sha256(changed_todo.encode()).hexdigest())
                with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                    with self.assertRaises(TaskFrontmatterError):
                        reconcile_missing_target(args, task, task.read_text(), task.stat())

    @staticmethod
    def complete_guarded_park_stop(args: StopArgs, session_id: str = "session") -> str:
        write_bound_close_proof(
            Path(args.bound_close_proof_path),
            Path(args.bound_close_audit_path),
            args.bound_close_proof_secret,
            args.bound_close_proof_commitment,
        )
        return session_id

    def write_park_case(self, root: Path, *, runat: str = "vl:2", todo_text: str | None = None) -> tuple[Path, str, Path, str, StatusArgs]:
        authority_dir = root / "manager_mail"
        authority_dir.mkdir(mode=0o700)
        authority = authority_dir / "halt.txt"
        authority.write_text(self.AUTHORITY_TEXT, encoding="utf-8")
        authority.chmod(0o600)
        excerpt = "".join(self.AUTHORITY_TEXT.splitlines(keepends=True)[2:6])
        envelope_text = (
            '<human_instruction authoritative="true" source="manager_mail/halt.txt:3-6">\n'
            f"{excerpt}</human_instruction>\n"
        )
        envelope = root / "vl_pause.md"
        envelope.write_text(envelope_text, encoding="utf-8")
        text = task_frontmatter(
            status="blocked",
            blocked_on="reviewed lifecycle helper cannot create targetless TODO custody while preserving historical runat",
            pending_items=("preserve first item", "preserve second item"),
            runat=runat,
        ) + "existing evidence\n"
        task = root / "vl_task.md"
        task.write_text(text, encoding="utf-8")
        original_todo = todo_text or (
            f"current:\nother.md vl:9\n\n"
            "low priority:\nslow.md vl:7\n\n"
            f"human pending:\nvl_task.md {runat}\nwaiting.md vl:6\n\n"
            "previous:\nold.md vl:8\n"
        )
        todo = root / "TODO.md"
        todo.write_text(original_todo, encoding="utf-8")
        audit_dir = root / "audit"
        audit_dir.mkdir(mode=0o700)
        args = StatusArgs(
            root,
            Path("vl_task.md"),
            "",
            "",
            park_unlinked=True,
            expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            expected_todo_sha256=hashlib.sha256(original_todo.encode()).hexdigest(),
            expected_pane_id="%42",
            authority_file=Path("manager_mail/halt.txt"),
            authority_lines=(3, 6),
            authority_sha256=hashlib.sha256(self.AUTHORITY_TEXT.encode()).hexdigest(),
            authority_envelope=Path("vl_pause.md"),
            authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
            audit_output=(audit_dir / "park.yaml").resolve(),
        )
        return task, text, todo, original_todo, args

    @staticmethod
    def archive_park_authority(root: Path, args: StatusArgs) -> StatusArgs:
        relative = Path("202607/manager_mail/halt.txt")
        archive_mail = root / relative.parent
        archive_mail.mkdir(parents=True)
        archive_mail.parent.chmod(0o755)
        archive_mail.chmod(0o755)
        (root / "manager_mail/halt.txt").rename(root / relative)
        envelope = root / "vl_pause.md"
        envelope_text = envelope.read_text(encoding="utf-8").replace(
            "manager_mail/halt.txt:3-6",
            f"{relative.as_posix()}:3-6",
        )
        envelope.write_text(envelope_text, encoding="utf-8")
        return replace(
            args,
            authority_file=relative,
            authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
        )

    @staticmethod
    def parked_low_priority_todo(todo_text: str, *, runat: str = "vl:2") -> str:
        source = f"human pending:\nvl_task.md {runat}\n"
        destination = "low priority:\n"
        if todo_text.count(source) != 1 or todo_text.count(destination) != 1:
            raise AssertionError("park fixture must have one exact source row and low-priority section")
        return todo_text.replace(source, "human pending:\n", 1).replace(
            destination,
            "low priority:\nvl_task.md\n",
            1,
        )

    def write_park_reattestation_case(self, root: Path) -> tuple[Path, str, Path, str, StatusArgs, str]:
        todo_text = "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\nvl_task.md vl:2\n"
        task, text, todo, _original_todo, args = self.write_park_case(root, todo_text=todo_text)
        session_id = "01a03a33-5aa7-7752-ba19-95d74a2910e3"
        text = text + "authority: manager_mail/halt.txt:3-6\n" + close_note("vl:2", session_id)
        task.write_text(text, encoding="utf-8")
        envelope = root / "vl_pause.md"
        with envelope.open("a", encoding="utf-8") as output:
            output.write("<agent_message>route under manager_mail/halt.txt:3-6</agent_message>\n")
        args = replace(
            args,
            session_id=session_id,
            expected_pane_id="",
            expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            authority_envelope_sha256=hashlib.sha256(envelope.read_bytes()).hexdigest(),
        )
        with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
            park_unlinked(args, task, text, task.stat())
        prior_text = args.audit_output.read_text(encoding="utf-8")
        archive = root / "202607"
        (archive / "manager_mail").mkdir(parents=True)
        archive.chmod(0o755)
        (archive / "manager_mail").chmod(0o755)
        (root / "manager_mail/halt.txt").rename(archive / "manager_mail/halt.txt")
        (root / "vl_pause.md").rename(archive / "vl_pause.md")
        envelope = archive / "vl_pause.md"
        envelope_text = envelope.read_text(encoding="utf-8").replace("manager_mail/halt.txt:3-6", "202607/manager_mail/halt.txt:3-6")
        envelope.write_text(envelope_text, encoding="utf-8")
        current_text = task.read_text(encoding="utf-8").replace("manager_mail/halt.txt:3-6", "202607/manager_mail/halt.txt:3-6")
        task.write_text(current_text, encoding="utf-8")
        current_todo = todo.read_text(encoding="utf-8")
        args = replace(
            args,
            park_unlinked=False,
            reattest_park_unlinked=True,
            expected_task_sha256=hashlib.sha256(current_text.encode()).hexdigest(),
            expected_todo_sha256=hashlib.sha256(current_todo.encode()).hexdigest(),
            expected_receipt_sha256=hashlib.sha256(prior_text.encode()).hexdigest(),
            authority_file=Path("202607/manager_mail/halt.txt"),
            authority_envelope=Path("202607/vl_pause.md"),
            authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
        )
        return task, current_text, todo, current_todo, args, prior_text

    def test_park_unlinked_reattestation_preserves_custody_and_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args, prior_text = self.write_park_reattestation_case(root)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")) as inspect_target,
                patch("omo_manager.omo_task_status.stop") as stop_owner,
            ):
                reattest_park_unlinked(args, task, text, task.stat())
            self.assertEqual(2, inspect_target.call_count)
            stop_owner.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            receipt = args.audit_output.read_text(encoding="utf-8")
            self.assertIn("operation: park-unlinked-re-attestation", receipt)
            self.assertIn(hashlib.sha256(prior_text.encode()).hexdigest(), receipt)

            v3_args = replace(
                args,
                expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                expected_receipt_sha256=hashlib.sha256(receipt.encode()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")) as inspect_target,
                patch("omo_manager.omo_task_status.stop") as stop_owner,
            ):
                reattest_park_unlinked(v3_args, task, text, task.stat())
            self.assertEqual(2, inspect_target.call_count)
            stop_owner.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            v3_receipt = args.audit_output.read_text(encoding="utf-8")
            self.assertIn("operation: park-unlinked-custody-re-attestation", v3_receipt)
            self.assertIn(hashlib.sha256(receipt.encode()).hexdigest(), v3_receipt)

    def test_park_unlinked_v2_migration_treats_todo_digest_as_historical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args, _prior_text = self.write_park_reattestation_case(root)
            with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
                reattest_park_unlinked(args, task, text, task.stat())
            receipt = args.audit_output.read_text(encoding="utf-8")
            record = yaml.safe_load(receipt)
            record["todo_sha256"] = "0" * 64
            changed = yaml.safe_dump(record, sort_keys=True)
            args.audit_output.write_text(changed, encoding="utf-8")
            migration_args = replace(
                args,
                expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                expected_receipt_sha256=hashlib.sha256(changed.encode()).hexdigest(),
            )
            with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
                reattest_park_unlinked(migration_args, task, text, task.stat())
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            migrated = yaml.safe_load(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual("0" * 64, migrated["prior_complete_receipt"]["todo_sha256"])

    def test_park_unlinked_v2_migration_rejects_rehashed_embedded_v1_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args, _prior_text = self.write_park_reattestation_case(root)
            with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
                reattest_park_unlinked(args, task, text, task.stat())
            record = yaml.safe_load(args.audit_output.read_text(encoding="utf-8"))
            record["prior_complete_receipt"]["task"] = "other.md"
            embedded = yaml.safe_dump(record["prior_complete_receipt"], sort_keys=True)
            record["prior_complete_receipt_sha256"] = hashlib.sha256(embedded.encode()).hexdigest()
            changed = yaml.safe_dump(record, sort_keys=True)
            args.audit_output.write_text(changed, encoding="utf-8")
            migration_args = replace(
                args,
                expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                expected_receipt_sha256=hashlib.sha256(changed.encode()).hexdigest(),
            )
            with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""), self.assertRaises(TaskFrontmatterError):
                reattest_park_unlinked(migration_args, task, text, task.stat())
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual(changed, args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_reattestation_rejects_drift_duplicate_owner_and_target(self) -> None:
        for case in ("task", "receipt", "malformed receipt", "envelope", "authority symlink", "owner", "live", "unknown"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args, prior_text = self.write_park_reattestation_case(root)
                if case == "task":
                    changed = text + "semantic drift\n"
                    task.write_text(changed, encoding="utf-8")
                    args = replace(args, expected_task_sha256=hashlib.sha256(changed.encode()).hexdigest())
                elif case == "receipt":
                    args = replace(args, expected_receipt_sha256="0" * 64)
                elif case == "malformed receipt":
                    malformed = prior_text + "state: complete\n"
                    args.audit_output.write_text(malformed, encoding="utf-8")
                    args = replace(args, expected_receipt_sha256=hashlib.sha256(malformed.encode()).hexdigest())
                    prior_text = malformed
                elif case == "envelope":
                    envelope = root / "202607/vl_pause.md"
                    changed_envelope = envelope.read_text().replace("</human_instruction>", "extra\n</human_instruction>")
                    envelope.write_text(changed_envelope, encoding="utf-8")
                    args = replace(args, authority_envelope_sha256=hashlib.sha256(changed_envelope.encode()).hexdigest())
                elif case == "authority symlink":
                    authority = root / "202607/manager_mail/halt.txt"
                    copied = root / "halt-copy.txt"
                    copied.write_text(authority.read_text(), encoding="utf-8")
                    authority.unlink()
                    authority.symlink_to(copied)
                elif case == "owner":
                    (root / "duplicate.md").write_text(task_frontmatter(status="blocked", runat="vl:2", pending_items=("work",)), encoding="utf-8")
                resolution = "%42" if case == "live" else None if case == "unknown" else ""
                with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=resolution), self.assertRaises(TaskFrontmatterError):
                    reattest_park_unlinked(args, task, task.read_text(encoding="utf-8"), task.stat())
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                self.assertEqual(prior_text, args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_stops_only_pinned_nonhuman_owner_and_preserves_task_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, original_todo, args = self.write_park_case(root)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch(
                    "omo_manager.omo_task_status.stop",
                    side_effect=lambda stop_args: self.complete_guarded_park_stop(
                        stop_args, "01a03702-dd9a-79c2-a1c4-3508f4918350"
                    ),
                ) as stop_owner,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))
            stop_owner.assert_called_once()
            stop_args = stop_owner.call_args.args[0]
            self.assertEqual("vl:2", stop_args.target)
            self.assertEqual("vl:2", stop_args.bound_symbolic_target)
            self.assertEqual("%42", stop_args.bound_pane_id)
            self.assertTrue(stop_args.no_feedback)
            self.assertEqual("vl_task.md", stop_args.task_file)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(self.parked_low_priority_todo(original_todo), todo.read_text(encoding="utf-8"))

    def test_park_unlinked_moves_proven_prior_stop_from_previous_without_pane_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo_text = (
                "current:\nother.md vl:9\n\nlow priority:\nslow.md vl:7\n\n"
                "human pending:\nwaiting.md vl:6\n\nprevious:\nvl_task.md vl:2\nold.md vl:8\n"
            )
            task, text, todo, _original_todo, args = self.write_park_case(root, todo_text=todo_text)
            session_id = "01a03a33-5aa7-7752-ba19-95d74a2910e3"
            text = text.rstrip("\n") + close_note("vl:2", session_id)
            task.write_text(text, encoding="utf-8")
            args = replace(
                args,
                session_id=session_id,
                expected_pane_id="",
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")),
                patch("omo_manager.omo_task_status.stop") as stop_owner,
            ):
                self.assertEqual("", park_unlinked(args, task, text, task.stat()))
            stop_owner.assert_not_called()
            expected = todo_text.replace("low priority:\n", "low priority:\nvl_task.md\n", 1).replace(
                "previous:\nvl_task.md vl:2\n",
                "previous:\n",
                1,
            )
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))
            self.assertIn("state: complete", args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_prior_stop_requires_exact_note_and_absent_target(self) -> None:
        for case, expected in (("note", "exact structured close note"), ("live", "historical target live")):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                todo_text = "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\nvl_task.md vl:2\n"
                task, text, todo, _original_todo, args = self.write_park_case(root, todo_text=todo_text)
                session_id = "01a03a33-5aa7-7752-ba19-95d74a2910e3"
                if case == "live":
                    text = text.rstrip("\n") + close_note("vl:2", session_id)
                    task.write_text(text, encoding="utf-8")
                args = replace(
                    args,
                    session_id=session_id,
                    expected_pane_id="",
                    expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                )
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%42"),
                    patch("omo_manager.omo_task_status.stop") as stop_owner,
                    self.assertRaisesRegex(TaskFrontmatterError, expected),
                ):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_prior_stop_recovers_after_completion_audit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo_text = "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\nvl_task.md vl:2\n"
            task, text, todo, _original_todo, args = self.write_park_case(root, todo_text=todo_text)
            session_id = "01a03a33-5aa7-7752-ba19-95d74a2910e3"
            text = text.rstrip("\n") + close_note("vl:2", session_id)
            task.write_text(text, encoding="utf-8")
            args = replace(
                args,
                session_id=session_id,
                expected_pane_id="",
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            )
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")),
                patch("omo_manager.omo_task_status.replace_private_audit", side_effect=OSError("audit completion failed")),
                self.assertRaisesRegex(OSError, "audit completion failed"),
            ):
                park_unlinked(args, task, text, task.stat())
            parked = todo_text.replace("low priority:\n", "low priority:\nvl_task.md\n", 1).replace(
                "previous:\nvl_task.md vl:2\n",
                "previous:\n",
                1,
            )
            self.assertEqual(parked, todo.read_text(encoding="utf-8"))
            self.assertIn("state: prior-stop-prepared", args.audit_output.read_text(encoding="utf-8"))
            retry_args = replace(args, expected_todo_sha256=hashlib.sha256(parked.encode()).hexdigest())
            with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
                self.assertEqual("", park_unlinked(retry_args, task, text, task.stat()))
            self.assertIn("state: complete", args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_rejects_stale_task_todo_or_pane_before_stop(self) -> None:
        for case, expected in (("task", "task bytes"), ("todo", "TODO bytes"), ("pane", "expected-pane-id")):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                if case == "task":
                    args = replace(args, expected_task_sha256="0" * 64)
                elif case == "todo":
                    args = replace(args, expected_todo_sha256="0" * 64)
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%41" if case == "pane" else "%42"),
                    patch("omo_manager.omo_task_status.stop") as stop_owner,
                    self.assertRaisesRegex(TaskFrontmatterError, expected),
                ):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_rejects_preexisting_close_proof_before_reservation_or_pane_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            proof = args.audit_output.with_name(f".{args.audit_output.name}.owner-stopped")
            proof.write_text("stale\n", encoding="utf-8")
            proof.chmod(0o600)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id") as pane_id,
                patch("omo_manager.omo_task_status.stop") as stop_owner,
                self.assertRaisesRegex(TaskFrontmatterError, "pre-existing close-proof artifact"),
            ):
                park_unlinked(args, task, text, task.stat())
            pane_id.assert_not_called()
            stop_owner.assert_not_called()
            self.assertFalse(args.audit_output.exists())
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_preserves_concurrent_task_or_todo_change_after_stop(self) -> None:
        for case in ("task", "todo"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                changed = text + "concurrent task evidence\n" if case == "task" else todo_text + "concurrent TODO evidence\n"

                def stop_owner(_args: StopArgs) -> str:
                    (task if case == "task" else todo).write_text(changed, encoding="utf-8")
                    return self.complete_guarded_park_stop(_args)

                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                    patch("omo_manager.omo_task_status.stop", side_effect=stop_owner),
                    self.assertRaisesRegex(TaskFrontmatterError, "task changed" if case == "task" else "TODO changed"),
                ):
                    park_unlinked(args, task, text, task.stat())
                self.assertEqual(changed, (task if case == "task" else todo).read_text(encoding="utf-8"))
                self.assertEqual(todo_text if case == "task" else text, (todo if case == "task" else task).read_text(encoding="utf-8"))

    def test_park_unlinked_stop_failure_never_infers_success_from_symbolic_disappearance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            symbolic_live = True

            def exact_symbolic(_target: str) -> str:
                return "%42" if symbolic_live else ""

            def fail_after_rebind(_args: StopArgs) -> str:
                nonlocal symbolic_live
                symbolic_live = False
                raise RuntimeError("stop rejected after pane moved to h-owned session")

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=exact_symbolic) as pane_id,
                patch("omo_manager.omo_task_status.stop", side_effect=fail_after_rebind),
                self.assertRaisesRegex(RuntimeError, "moved to h-owned"),
            ):
                park_unlinked(args, task, text, task.stat())
            self.assertEqual(1, pane_id.call_count)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertIn("state: prepared", args.audit_output.read_text(encoding="utf-8"))

    def test_park_target_snapshot_never_addresses_the_symbolic_target(self) -> None:
        result = subprocess.CompletedProcess(
            ["tmux"],
            0,
            "vl\t2\t0\t0\t%41\nvl\t2\t1\t1\t%42\nhvl\t2\t1\t1\t%99\n",
            "",
        )
        with patch("omo_manager.omo_task_status.tmux", return_value=result) as tmux_call:
            self.assertEqual("%42", park_target_pane_id("vl:2"))
            self.assertEqual("%41", park_target_pane_id("vl:2.0"))
        for call in tmux_call.call_args_list:
            self.assertEqual("list-panes", call.args[0][0])
            self.assertNotIn("-t", call.args[0])

    def test_park_target_snapshot_rejects_failed_malformed_or_ambiguous_queries(self) -> None:
        cases = (
            subprocess.CompletedProcess(["tmux"], 1, "", "server unavailable"),
            subprocess.CompletedProcess(["tmux"], 0, "truncated snapshot\n", ""),
            subprocess.CompletedProcess(
                ["tmux"],
                0,
                "vl\t2\t0\t1\t%41\nvl\t2\t1\t1\t%42\n",
                "",
            ),
        )
        for result in cases:
            with self.subTest(result=result), patch("omo_manager.omo_task_status.tmux", return_value=result):
                self.assertIsNone(park_target_pane_id("vl:2"))

    def test_park_unlinked_prior_stop_rejects_invalid_tmux_snapshot_without_mutation(self) -> None:
        cases = (
            subprocess.CompletedProcess(["tmux"], 1, "", "server unavailable"),
            subprocess.CompletedProcess(["tmux"], 0, "truncated snapshot\n", ""),
            subprocess.CompletedProcess(
                ["tmux"],
                0,
                "vl\t2\t0\t1\t%41\nvl\t2\t1\t1\t%42\n",
                "",
            ),
        )
        for result in cases:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                todo_text = "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\nvl_task.md vl:2\n"
                task, text, todo, _original_todo, args = self.write_park_case(root, todo_text=todo_text)
                session_id = "01a03a33-5aa7-7752-ba19-95d74a2910e3"
                text = text.rstrip("\n") + close_note("vl:2", session_id)
                task.write_text(text, encoding="utf-8")
                args = replace(
                    args,
                    session_id=session_id,
                    expected_pane_id="",
                    expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                )
                with (
                    patch("omo_manager.omo_task_status.tmux", return_value=result),
                    self.assertRaisesRegex(TaskFrontmatterError, "unambiguous tmux target snapshot"),
                ):
                    park_unlinked(args, task, text, task.stat())
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                self.assertFalse(args.audit_output.exists())

    def test_park_unlinked_owner_stopped_recovery_rejects_invalid_tmux_snapshot(self) -> None:
        cases = (
            subprocess.CompletedProcess(["tmux"], 1, "", "server unavailable"),
            subprocess.CompletedProcess(["tmux"], 0, "truncated snapshot\n", ""),
            subprocess.CompletedProcess(
                ["tmux"],
                0,
                "vl\t2\t0\t1\t%41\nvl\t2\t1\t1\t%42\n",
                "",
            ),
        )
        for result in cases:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                    patch("omo_manager.omo_task_status.stop", side_effect=self.complete_guarded_park_stop),
                    patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=OSError("TODO write failed")),
                    self.assertRaisesRegex(OSError, "TODO write failed"),
                ):
                    park_unlinked(args, task, text, task.stat())
                audit_before = args.audit_output.read_bytes()
                with (
                    patch("omo_manager.omo_task_status.tmux", return_value=result),
                    self.assertRaisesRegex(TaskFrontmatterError, "unambiguous tmux target snapshot"),
                ):
                    park_unlinked(args, task, text, task.stat())
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                self.assertEqual(audit_before, args.audit_output.read_bytes())

    def test_park_unlinked_recovers_after_todo_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch("omo_manager.omo_task_status.stop", side_effect=self.complete_guarded_park_stop) as stop_owner,
                patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=OSError("write failed")),
                self.assertRaisesRegex(OSError, "write failed"),
            ):
                park_unlinked(args, task, text, task.stat())
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertIn("state: owner-stopped", args.audit_output.read_text(encoding="utf-8"))
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")),
                patch("omo_manager.omo_task_status.stop") as retry_stop,
            ):
                self.assertEqual("", park_unlinked(args, task, text, task.stat()))
            stop_owner.assert_called_once()
            retry_stop.assert_not_called()
            self.assertEqual(self.parked_low_priority_todo(todo_text), todo.read_text(encoding="utf-8"))
            self.assertIn("state: complete", args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_recovers_when_first_audit_transition_fails_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch("omo_manager.omo_task_status.stop", side_effect=self.complete_guarded_park_stop),
                patch("omo_manager.omo_task_status.replace_private_audit", side_effect=OSError("audit transition failed")),
                self.assertRaisesRegex(OSError, "audit transition failed"),
            ):
                park_unlinked(args, task, text, task.stat())
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertIn("state: prepared", args.audit_output.read_text(encoding="utf-8"))
            proof = args.audit_output.with_name(f".{args.audit_output.name}.owner-stopped")
            self.assertTrue(proof.is_file())
            with patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""):
                self.assertEqual("", park_unlinked(args, task, text, task.stat()))
            self.assertEqual(self.parked_low_priority_todo(todo_text), todo.read_text(encoding="utf-8"))
            self.assertIn("state: complete", args.audit_output.read_text(encoding="utf-8"))
            self.assertFalse(proof.exists())

    def test_park_unlinked_recovers_and_preserves_concurrent_todo_change_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            changed = todo_text + "concurrent TODO evidence\n"

            def stop_owner(_args: StopArgs) -> str:
                todo.write_text(changed, encoding="utf-8")
                return self.complete_guarded_park_stop(_args)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch("omo_manager.omo_task_status.stop", side_effect=stop_owner),
                self.assertRaisesRegex(TaskFrontmatterError, "TODO changed"),
            ):
                park_unlinked(args, task, text, task.stat())
            retry_args = replace(args, expected_todo_sha256=hashlib.sha256(changed.encode()).hexdigest())
            with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
                self.assertEqual("", park_unlinked(retry_args, task, text, task.stat()))
            self.assertEqual(self.parked_low_priority_todo(changed), todo.read_text(encoding="utf-8"))

    def test_park_unlinked_recovers_when_completion_audit_write_fails_after_todo_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            calls = 0

            def fail_completion(audit_path: Path, expected: str, updated: str) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("completion audit failed")
                replace_private_audit(audit_path, expected, updated)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch("omo_manager.omo_task_status.stop", side_effect=self.complete_guarded_park_stop),
                patch("omo_manager.omo_task_status.replace_private_audit", side_effect=fail_completion),
                self.assertRaisesRegex(OSError, "completion audit failed"),
            ):
                park_unlinked(args, task, text, task.stat())
            parked_todo = self.parked_low_priority_todo(todo_text)
            self.assertEqual(parked_todo, todo.read_text(encoding="utf-8"))
            self.assertIn("state: owner-stopped", args.audit_output.read_text(encoding="utf-8"))
            retry_args = replace(args, expected_todo_sha256=hashlib.sha256(parked_todo.encode()).hexdigest())
            with patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("", "")):
                self.assertEqual("", park_unlinked(retry_args, task, text, task.stat()))
            self.assertIn("state: complete", args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_rejects_manager_or_unbound_authority_envelope(self) -> None:
        for case, expected in (("manager", "non-manager"), ("envelope", "envelope")):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                if case == "manager":
                    text = text.replace("is_manager: false", "is_manager: true")
                    task.write_text(text, encoding="utf-8")
                    (root / "child.md").write_text(
                        task_frontmatter(status="running", runat="vl-child:3", managerat="vl:2")
                        + "child evidence\n",
                        encoding="utf-8",
                    )
                    args = replace(args, expected_task_sha256=hashlib.sha256(text.encode()).hexdigest())
                else:
                    envelope = root / "vl_pause.md"
                    changed_envelope = envelope.read_text(encoding="utf-8").replace('authoritative="true"', 'authoritative="false"')
                    envelope.write_text(changed_envelope, encoding="utf-8")
                    args = replace(args, authority_envelope_sha256=hashlib.sha256(changed_envelope.encode()).hexdigest())
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id") as pane_id,
                    patch("omo_manager.omo_task_status.stop") as stop_owner,
                    self.assertRaisesRegex(TaskFrontmatterError, expected),
                ):
                    park_unlinked(args, task, text, task.stat())
                pane_id.assert_not_called()
                stop_owner.assert_not_called()

    def test_park_unlinked_authority_envelope_accepts_intake_newline_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            authority = root / "manager_mail/halt.txt"
            crlf_authority = authority.read_bytes().replace(b"\n", b"\r\n")
            authority.write_bytes(crlf_authority)
            args = replace(args, authority_sha256=hashlib.sha256(crlf_authority).hexdigest())
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch("omo_manager.omo_task_status.stop", side_effect=self.complete_guarded_park_stop),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))
            self.assertEqual(text.encode(), task.read_bytes())
            self.assertEqual(self.parked_low_priority_todo(todo_text), todo.read_text(encoding="utf-8"))

    def test_park_unlinked_accepts_digest_bound_monthly_archive_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            args = self.archive_park_authority(root, args)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=("%42", "")),
                patch("omo_manager.omo_task_status.stop", side_effect=self.complete_guarded_park_stop),
            ):
                self.assertEqual("session", park_unlinked(args, task, text, task.stat()))
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(self.parked_low_priority_todo(todo_text), todo.read_text(encoding="utf-8"))
            self.assertIn("authority_source: 202607/manager_mail/halt.txt:3-6", args.audit_output.read_text(encoding="utf-8"))

    def test_park_unlinked_monthly_archive_rejects_traversal_and_malformed_layouts(self) -> None:
        paths = (
            Path("202607/manager_mail/../manager_mail/halt.txt"),
            Path("202607/halt.txt"),
            Path("archive/manager_mail/halt.txt"),
            Path("٢٠٢٦07/manager_mail/halt.txt"),
            Path("202613/manager_mail/halt.txt"),
            Path("202607/manager_mail/nested/halt.txt"),
        )
        for authority_file in paths:
            with self.subTest(authority_file=authority_file), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                args = replace(self.archive_park_authority(root, args), authority_file=authority_file)
                with patch("omo_manager.omo_task_status.stop") as stop_owner, self.assertRaisesRegex(
                    TaskFrontmatterError,
                    "under the task root",
                ):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_monthly_archive_rejects_symlinked_components(self) -> None:
        for component in ("month", "manager_mail", "file"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                args = self.archive_park_authority(root, args)
                month = root / "202607"
                mail = month / "manager_mail"
                authority = mail / "halt.txt"
                if component == "month":
                    real = root / "archive-month"
                    month.rename(real)
                    month.symlink_to(real.name, target_is_directory=True)
                elif component == "manager_mail":
                    real = month / "archive-mail"
                    mail.rename(real)
                    mail.symlink_to(real.name, target_is_directory=True)
                else:
                    real = mail / "archive-authority.txt"
                    authority.rename(real)
                    authority.symlink_to(real.name)
                with patch("omo_manager.omo_task_status.stop") as stop_owner, self.assertRaisesRegex(
                    TaskFrontmatterError,
                    "without symlinks",
                ):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_monthly_archive_retains_digest_and_envelope_binding(self) -> None:
        for case in ("digest", "envelope"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                args = self.archive_park_authority(root, args)
                if case == "digest":
                    args = replace(args, authority_sha256="0" * 64)
                    expected = "authority source"
                else:
                    envelope = root / "vl_pause.md"
                    changed = envelope.read_text(encoding="utf-8").replace(
                        "202607/manager_mail/halt.txt:3-6",
                        "manager_mail/halt.txt:3-6",
                    )
                    envelope.write_text(changed, encoding="utf-8")
                    args = replace(args, authority_envelope_sha256=hashlib.sha256(changed.encode()).hexdigest())
                    expected = "authority envelope"
                with patch("omo_manager.omo_task_status.stop") as stop_owner, self.assertRaisesRegex(
                    TaskFrontmatterError,
                    expected,
                ):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_rejects_owner_stopped_audit_without_matching_close_proof(self) -> None:
        for case in ("missing", "mismatched", "stale symlink"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                secret = "a" * 64
                commitment = hashlib.sha256(secret.encode()).hexdigest()
                args.audit_output.write_text(
                    park_audit_record(
                        args,
                        task,
                        "vl:2",
                        "manager_mail/halt.txt:3-6",
                        "vl_pause.md",
                        args.expected_todo_sha256,
                        commitment,
                        "owner-stopped",
                    ),
                    encoding="utf-8",
                )
                args.audit_output.chmod(0o600)
                proof = args.audit_output.with_name(f".{args.audit_output.name}.owner-stopped")
                if case == "mismatched":
                    proof.write_text("b" * 64 + "\n", encoding="utf-8")
                    proof.chmod(0o600)
                elif case == "stale symlink":
                    stale = root / "stale-proof"
                    stale.write_text(secret + "\n", encoding="utf-8")
                    stale.chmod(0o600)
                    proof.symlink_to(stale)
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
                    patch("omo_manager.omo_task_status.stop") as stop_owner,
                    self.assertRaisesRegex(TaskFrontmatterError, "guarded-close proof"),
                ):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_recovers_owner_stopped_audit_with_matching_close_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_park_case(root)
            secret = "a" * 64
            commitment = hashlib.sha256(secret.encode()).hexdigest()
            args.audit_output.write_text(
                park_audit_record(
                    args,
                    task,
                    "vl:2",
                    "manager_mail/halt.txt:3-6",
                    "vl_pause.md",
                    args.expected_todo_sha256,
                    commitment,
                    "owner-stopped",
                ),
                encoding="utf-8",
            )
            args.audit_output.chmod(0o600)
            proof = args.audit_output.with_name(f".{args.audit_output.name}.owner-stopped")
            proof.write_text(secret + "\n", encoding="utf-8")
            proof.chmod(0o600)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop") as stop_owner,
            ):
                self.assertEqual("", park_unlinked(args, task, text, task.stat()))
            stop_owner.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(self.parked_low_priority_todo(todo_text), todo.read_text(encoding="utf-8"))
            self.assertIn("state: complete", args.audit_output.read_text(encoding="utf-8"))
            self.assertFalse(proof.exists())

    def test_park_unlinked_rejects_unsafe_or_wrong_authority_and_conflicting_owner(self) -> None:
        for case, expected in (("digest", "authority source"), ("words", "authority envelope"), ("symlink", "direct file"), ("owner", "sole authoritative owner")):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_park_case(root)
                authority = root / "manager_mail" / "halt.txt"
                if case == "digest":
                    args = replace(args, authority_sha256="0" * 64)
                elif case == "words":
                    replacement = "Subject: discussion\n\nDo not change any pane.\n"
                    authority.write_text(replacement, encoding="utf-8")
                    args = replace(args, authority_lines=(3, 3), authority_sha256=hashlib.sha256(replacement.encode()).hexdigest())
                elif case == "symlink":
                    replacement = root / "manager_mail" / "real.txt"
                    authority.rename(replacement)
                    authority.symlink_to(replacement.name)
                else:
                    (root / "owner.md").write_text(task_frontmatter(status="running", runat="vl:2") + "owner\n", encoding="utf-8")
                with patch("omo_manager.omo_task_status.stop") as stop_owner, self.assertRaisesRegex(TaskFrontmatterError, expected):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_rejects_human_target_or_noncanonical_custody_without_pane_input(self) -> None:
        cases = (
            (
                "human",
                "current:\n\nlow priority:\n\nhuman pending:\nvl_task.md hvl:2\n\nprevious:\n",
                "non-human",
            ),
            (
                "current",
                "current:\nvl_task.md vl:2\n\nlow priority:\n\nhuman pending:\n\nprevious:\n",
                "human pending",
            ),
            (
                "annotation",
                "current:\n\nlow priority:\n\nhuman pending:\nvl_task.md vl:2 keep\n\nprevious:\n",
                "canonical TODO row in human pending",
            ),
            (
                "duplicate",
                "current:\n\nlow priority:\n\nhuman pending:\nvl_task.md vl:2\nvl_task.md vl:2\n\nprevious:\n",
                "exactly one task row",
            ),
            (
                "source header whitespace",
                "current:\n\nlow priority:\n\n human pending: \nvl_task.md vl:2\n\nprevious:\n",
                "canonical TODO section headers",
            ),
        )
        for case, todo_text, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runat = "hvl:2" if case == "human" else "vl:2"
                task, text, todo, original_todo, args = self.write_park_case(root, runat=runat, todo_text=todo_text)
                with patch("omo_manager.omo_task_status.stop") as stop_owner, self.assertRaisesRegex(TaskFrontmatterError, expected):
                    park_unlinked(args, task, text, task.stat())
                stop_owner.assert_not_called()
                self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_rejects_ambiguous_low_priority_destination_without_pane_input(self) -> None:
        cases = (
            (
                "missing destination",
                "current:\n\nhuman pending:\nvl_task.md vl:2\n\nprevious:\n",
                "low.priority",
            ),
            (
                "duplicate destination",
                "current:\n\nlow priority:\n\nlow priority:\n\nhuman pending:\nvl_task.md vl:2\n\nprevious:\n",
                "low.priority",
            ),
            (
                "already duplicated at destination",
                "current:\n\nlow priority:\nvl_task.md\n\nhuman pending:\nvl_task.md vl:2\n\nprevious:\n",
                "exactly one task row",
            ),
            (
                "targetless source",
                "current:\n\nlow priority:\n\nhuman pending:\nvl_task.md\n\nprevious:\n",
                "canonical TODO row in human pending",
            ),
            (
                "destination header whitespace",
                "current:\n\n low priority:\n\nhuman pending:\nvl_task.md vl:2\n\nprevious:\n",
                "canonical TODO section headers",
            ),
        )
        for case, todo_text, expected in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, original_todo, args = self.write_park_case(root, todo_text=todo_text)
                with (
                    patch("omo_manager.omo_task_status.exact_pane_id") as pane_id,
                    patch("omo_manager.omo_task_status.stop") as stop_owner,
                    self.assertRaisesRegex(TaskFrontmatterError, expected),
                ):
                    park_unlinked(args, task, text, task.stat())
                pane_id.assert_not_called()
                stop_owner.assert_not_called()
                self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_park_unlinked_parser_requires_complete_authority_and_stale_state_assertions(self) -> None:
        complete = [
            "--root",
            "/tmp/work",
            "--park-unlinked",
            "--expected-task-sha256",
            "a" * 64,
            "--expected-todo-sha256",
            "b" * 64,
            "--expected-pane-id",
            "%42",
            "--authority-file",
            "manager_mail/halt.txt",
            "--authority-lines",
            "3-6",
            "--authority-sha256",
            "c" * 64,
            "--authority-envelope",
            "vl_pause.md",
            "--authority-envelope-sha256",
            "d" * 64,
            "--audit-output",
            "/tmp/park-audit.yaml",
            "task.md",
        ]
        args = parse_args(complete)
        self.assertTrue(args.park_unlinked)
        self.assertEqual((3, 6), args.authority_lines)
        prior = complete.copy()
        pane_option = prior.index("--expected-pane-id")
        del prior[pane_option : pane_option + 2]
        prior[pane_option:pane_option] = ["--session-id", "01a03a33-5aa7-7752-ba19-95d74a2910e3"]
        prior_args = parse_args(prior)
        self.assertEqual("", prior_args.expected_pane_id)
        self.assertEqual("01a03a33-5aa7-7752-ba19-95d74a2910e3", prior_args.session_id)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(complete[:-2] + ["task.md"])
        foreign_inputs = (
            ("--stale-sha256", "e" * 64),
            ("--replacement-sha256", "e" * 64),
            ("--replacement-status", "running"),
            ("--protected-target", "vl:9"),
            ("--stopped-evidence", "evidence"),
            ("--replacement-pane-evidence", "evidence"),
            ("--pane-id", "%9"),
            ("--terminal-evidence", "evidence"),
            ("--task-sha256", "e" * 64),
            ("--historical-commit", "e" * 40),
            ("--source-sha256", "e" * 64),
        )
        for option, value in foreign_inputs:
            with self.subTest(option=option), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args([*complete[:-1], option, value, "task.md"])
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--expected-pane-id", "%42", "task.md", "blocked", "--blocked-on", "human"])

    def test_park_unlinked_reattestation_parser_requires_prior_receipt_and_session(self) -> None:
        complete = [
            "--root", "/tmp/work", "--reattest-park-unlinked",
            "--expected-task-sha256", "a" * 64,
            "--expected-todo-sha256", "b" * 64,
            "--expected-receipt-sha256", "c" * 64,
            "--session-id", "01a03a33-5aa7-7752-ba19-95d74a2910e3",
            "--authority-file", "202607/manager_mail/halt.txt",
            "--authority-lines", "3-6", "--authority-sha256", "d" * 64,
            "--authority-envelope", "202607/vl_pause.md",
            "--authority-envelope-sha256", "e" * 64,
            "--audit-output", "/tmp/park-audit.yaml", "task.md",
        ]
        args = parse_args(complete)
        self.assertTrue(args.reattest_park_unlinked)
        self.assertFalse(args.park_unlinked)
        for option in ("--expected-receipt-sha256", "--session-id", "--authority-envelope"):
            candidate = complete.copy()
            index = candidate.index(option)
            del candidate[index : index + 2]
            with self.subTest(option=option), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args(candidate)

    def test_normal_done_parser_preserves_hash_bound_human_close_authority(self) -> None:
        args = parse_args(
            [
                "--root",
                "/tmp/work_logs",
                "--human-close-authorization-source",
                "manager_mail/authority.txt",
                "--human-close-authorization-sha256",
                "a" * 64,
                "task.md",
                "done",
            ]
        )
        self.assertEqual("manager_mail/authority.txt", args.human_close_authorization_source)
        self.assertEqual("a" * 64, args.human_close_authorization_sha256)

    def write_done_live_close_case(self, root: Path) -> tuple[Path, str, Path, str, StatusArgs]:
        task = root / "task.md"
        text = task_frontmatter(status="done", runat="wl:2") + "body\n"
        task.write_text(text, encoding="utf-8")
        todo = root / "TODO.md"
        todo_text = "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n"
        todo.write_text(todo_text, encoding="utf-8")
        private = root / "private"
        private.mkdir(mode=0o700)
        args = StatusArgs(
            root,
            Path("task.md"),
            "done",
            "",
            close_done_live_no_mail=True,
            active_target="wl:2",
            manager_target="wl:1",
            expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
            expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
            expected_pane_id="%42",
            expected_pane_pid=4242,
            expected_pane_start_ticks=73,
            expected_session_id="019e9ed9-6262-71c0-b4b3-72ffd4182e98",
            terminal_evidence="accepted-report-receipt-token",
            audit_output=(private / "done-live-close.json").resolve(),
        )
        return task, text, todo, todo_text, args

    def write_report_transaction(
        self,
        private: Path,
        task: Path,
        target: str,
        body: bytes,
        label: str,
    ) -> dict[str, str]:
        report = private / f"{label}.md"
        report.write_bytes(body)
        os.chmod(report, 0o600)
        report_sha256 = hashlib.sha256(body).hexdigest()
        replay_id = hashlib.sha256(f"{label}-replay".encode()).hexdigest()
        commitment = private / f"{replay_id}.commitment"
        envelope = private / f"{label}-envelope.md"
        transfer = {
            "authority": {"source_task": str(task), "producer_target": target},
            "routing": {"task": str(task)},
        }
        record: dict[str, object] = {
            "allocation": {
                "file": str(report),
                "file_at_submission": {"sha256": report_sha256, "size": len(body)},
            },
            "preflight": {"records": {"private_envelope": str(envelope), "producer": str(task)}},
            "replay_id": replay_id,
            "schema": "omo-report-transaction-commitment/v2",
            "transfer": transfer,
        }
        record["commitment_id"] = hashlib.sha256(
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        commitment.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(commitment, 0o600)
        attached = {**transfer, "commitment_id": record["commitment_id"]}
        attached["transfer_id"] = hashlib.sha256(
            json.dumps(attached, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        envelope.write_bytes(
            (
                f"(sent from agent via omo_report.sh tmux={target} time=00:00 task-file={task.name})\n"
                f"[message-sha256: {report_sha256}]\n"
                "[omo-report-owner-prefix: manager-path-sha256=" + "a" * 64 + " sha256=" + "b" * 64 + " size-bytes=1 separator-bytes=1]\n"
                f"[omo-transfer: {json.dumps(attached, sort_keys=True, separators=(',', ':'))}]\n"
                "message:\n"
            ).encode()
            + body
        )
        os.chmod(envelope, 0o600)
        return {
            "commitment": str(commitment),
            "commitment_sha256": hashlib.sha256(commitment.read_bytes()).hexdigest(),
            "envelope": str(envelope),
            "envelope_sha256": hashlib.sha256(envelope.read_bytes()).hexdigest(),
            "report": str(report),
            "report_sha256": report_sha256,
        }

    def write_manager_consumed_receipt(self, root: Path, task: Path, args: StatusArgs) -> StatusArgs:
        private = args.audit_output.parent
        terminal_receipt_sha256 = "d" * 64
        worker = self.write_report_transaction(
            private, task, args.active_target,
            f"terminal receipt {terminal_receipt_sha256}\n".encode(), "worker-report",
        )
        args = replace(args, terminal_evidence=worker["report_sha256"])
        manager_task = root / "manager.md"
        manager_task.write_text(
            task_frontmatter(status="long_running", runat="wl:1", managerat="wl:9", is_manager=True),
            encoding="utf-8",
        )
        manager_body = (
            f"task {task.name}; target {args.active_target}; worker report {args.terminal_evidence}; "
            f"terminal receipt {terminal_receipt_sha256}; task status done; ordered queue empty; "
            f"TODO placement previous; pane {args.expected_pane_id}; "
            f"PID/start ticks {args.expected_pane_pid}/{args.expected_pane_start_ticks}; "
            f"Codex session {args.expected_session_id}; reserved close-audit SHA-256 "
            f"{hashlib.sha256(render_done_live_close_audit(args, task, DoneLiveCloseAudit('reserved')).encode()).hexdigest()}; "
            "and no Human mail.\n"
        ).encode()
        manager = self.write_report_transaction(private, manager_task, "wl:1", manager_body, "manager-report")
        receipt: dict[str, object] = {
            "accepted": True,
            "audit": str(args.audit_output),
            "audit_sha256": hashlib.sha256(
                render_done_live_close_audit(args, task, DoneLiveCloseAudit("reserved")).encode()
            ).hexdigest(),
            "manager_acceptance": {
                "task": str(manager_task),
                "task_sha256": hashlib.sha256(manager_task.read_bytes()).hexdigest(),
                "target": "wl:1",
                "transaction": manager,
            },
            "no_mail": True,
            "pane_id": args.expected_pane_id,
            "pane_pid": args.expected_pane_pid,
            "pane_start_ticks": args.expected_pane_start_ticks,
            "schema": "omo-done-live-manager-consumed/v1",
            "session_id": args.expected_session_id,
            "target": args.active_target,
            "task": task.name,
            "task_sha256": args.expected_task_sha256,
            "terminal_evidence_sha256": hashlib.sha256(worker["report_sha256"].encode()).hexdigest(),
            "terminal_receipt_sha256": terminal_receipt_sha256,
            "todo_sha256": args.expected_todo_sha256,
            "worker_report": worker,
        }
        receipt["receipt_id"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt_path = private / "manager-consumed.json"
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(receipt_path, 0o600)
        return replace(
            args,
            manager_consumed_report_receipt=receipt_path,
            manager_consumed_report_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        )

    def test_done_live_no_mail_parser_requires_bound_close_evidence(self) -> None:
        complete = [
            "--root", "/tmp/work_logs", "--close-done-live-no-mail",
            "--active-target", "wl:2", "--manager-target", "wl:1",
            "--expected-task-sha256", "a" * 64,
            "--expected-todo-sha256", "b" * 64,
            "--expected-pane-id", "%42", "--expected-pane-pid", "4242",
            "--expected-pane-start-ticks", "73",
            "--expected-session-id", "019e9ed9-6262-71c0-b4b3-72ffd4182e98",
            "--terminal-evidence", "accepted-report-receipt-token",
            "--audit-output", "/tmp/done-live-close.json", "task.md",
        ]
        args = parse_args(complete)
        self.assertTrue(args.close_done_live_no_mail)
        self.assertEqual("done", args.status)
        for option in (
            "--active-target", "--manager-target", "--expected-task-sha256",
            "--expected-todo-sha256", "--expected-pane-id", "--expected-pane-pid",
            "--expected-pane-start-ticks", "--expected-session-id",
            "--terminal-evidence", "--audit-output",
        ):
            candidate = complete.copy()
            index = candidate.index(option)
            del candidate[index : index + 2]
            with self.subTest(option=option), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args(candidate)
        human_target = complete.copy()
        human_target[human_target.index("--active-target") + 1] = "hwork:2"
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(human_target)
        consumed = complete[:-1] + [
            "--manager-consumed-report-receipt", "/tmp/consumed.json",
            "--manager-consumed-report-receipt-sha256", "c" * 64,
            "task.md",
        ]
        parsed = parse_args(consumed)
        self.assertEqual(Path("/tmp/consumed.json"), parsed.manager_consumed_report_receipt)
        missing_digest = consumed.copy()
        index = missing_digest.index("--manager-consumed-report-receipt-sha256")
        del missing_digest[index : index + 2]
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(missing_digest)

    def test_no_mail_close_helpers_reject_each_other_on_direct_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, _protected, todo, args, task_text, _protected_text, todo_text = write_active_task_tree_fixture(root)
            conflicting = replace(args, close_done_live_no_mail=True)
            with self.assertRaisesRegex(TaskFrontmatterError, "arguments do not satisfy"):
                close_active_task_tree_no_mail(conflicting, task, task_text, task.stat())
            self.assertEqual(task_text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)
            conflicting = replace(args, close_active_task_tree_no_mail=True)
            with self.assertRaisesRegex(TaskFrontmatterError, "arguments do not satisfy"):
                close_done_live_no_mail(conflicting, task, text, task.stat())
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_live_no_mail_parser_requires_exact_cas_evidence(self) -> None:
        complete = [
            "--root", "/tmp/work_logs", "--complete-live-no-mail",
            "--active-target", "wl:2",
            "--manager-target", "wl:1",
            "--expected-task-sha256", "a" * 64,
            "--expected-todo-sha256", "b" * 64,
            "--expected-pane-id", "%42",
            "task.md",
        ]
        args = parse_args(complete)
        self.assertTrue(args.complete_live_no_mail)
        self.assertEqual("done", args.status)
        self.assertEqual("wl:2", args.active_target)
        self.assertEqual("wl:1", args.manager_target)
        self.assertEqual("%42", args.expected_pane_id)
        for option in ("--active-target", "--manager-target", "--expected-task-sha256", "--expected-todo-sha256", "--expected-pane-id"):
            candidate = complete.copy()
            index = candidate.index(option)
            del candidate[index : index + 2]
            with self.subTest(option=option), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args(candidate)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args([*complete, "done"])
        human_manager = complete.copy()
        human_manager[human_manager.index("--manager-target") + 1] = "hmanager:1"
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(human_manager)

    def test_live_no_mail_completion_changes_only_task_and_todo_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter(status="running", runat="wl:2") + "body\n"
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nlow priority:\n\nhuman pending:\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            unrelated = root / "notes.txt"
            unrelated.write_text("preserve\n", encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                complete_live_no_mail=True,
                active_target="wl:2",
                manager_target="wl:1",
                expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                expected_pane_id="%42",
            )
            output = io.StringIO()
            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value="%42") as pane,
                patch("omo_manager.omo_task_status.require_owner_completion") as email,
                patch("omo_manager.omo_task_status.stop_done_agent") as stop,
                patch("omo_manager.omo_task_status.record_close") as record,
                patch("omo_manager.omo_task_status.blocking_request") as blocking,
                redirect_stdout(output),
            ):
                self.assertEqual(0, run(args))
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual((), metadata.pending_task_items)
            self.assertTrue(task.read_text(encoding="utf-8").endswith("body\n"))
            self.assertEqual(
                "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n",
                todo.read_text(encoding="utf-8"),
            )
            self.assertEqual("preserve\n", unrelated.read_text(encoding="utf-8"))
            self.assertIn("without email or pane mutation", output.getvalue())
            self.assertEqual(2, pane.call_count)
            pane.assert_called_with("wl:2")
            email.assert_not_called()
            stop.assert_not_called()
            record.assert_not_called()
            blocking.assert_not_called()

    def test_live_no_mail_completion_rejects_nonmatching_task_and_todo_state(self) -> None:
        cases = (
            ("blocked", task_frontmatter(status="blocked", blocked_on="wait", runat="wl:2") + "body\n", "current:\ntask.md wl:2\n\nprevious:\n"),
            ("queue", task_frontmatter(status="running", runat="wl:2", pending_items=("work",)) + "body\n", "current:\ntask.md wl:2\n\nprevious:\n"),
            ("manager", task_frontmatter(status="running", runat="wl:2", is_manager=True) + "body\n", "current:\ntask.md wl:2\n\nprevious:\n"),
            ("wrong manager", task_frontmatter(status="running", runat="wl:2", managerat="wl:9") + "body\n", "current:\ntask.md wl:2\n\nprevious:\n"),
            ("human manager", task_frontmatter(status="running", runat="wl:2", managerat="hmanager:1") + "body\n", "current:\ntask.md wl:2\n\nprevious:\n"),
            ("wrong section", task_frontmatter(status="running", runat="wl:2") + "body\n", "current:\n\nlow priority:\ntask.md wl:2\n\nprevious:\n"),
            ("wrong target", task_frontmatter(status="running", runat="wl:2") + "body\n", "current:\ntask.md wl:3\n\nprevious:\n"),
            ("duplicate row", task_frontmatter(status="running", runat="wl:2") + "body\n", "current:\ntask.md wl:2\ntask.md wl:2\n\nprevious:\n"),
            ("aliased row", task_frontmatter(status="running", runat="wl:2") + "body\n", "current:\n`task.md` wl:2\n\nprevious:\n"),
            ("annotated row", task_frontmatter(status="running", runat="wl:2") + "body\n", "current:\ntask.md wl:2 note\n\nprevious:\n"),
        )
        for label, text, todo_text in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "task.md"
                task.write_text(text, encoding="utf-8")
                todo = root / "TODO.md"
                todo.write_text(todo_text, encoding="utf-8")
                args = StatusArgs(
                    root,
                    Path("task.md"),
                    "done",
                    "",
                    complete_live_no_mail=True,
                    active_target="wl:2",
                    manager_target="hmanager:1" if label == "human manager" else "wl:1",
                    expected_task_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                    expected_pane_id="%42",
                )
                with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%42"), redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_live_no_mail_completion_rejects_digest_owner_pane_and_write_races(self) -> None:
        for case in ("task digest", "todo digest", "duplicate owner", "pane", "task race", "todo race", "task write"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "task.md"
                text = task_frontmatter(status="running", runat="wl:2") + "body\n"
                task.write_text(text, encoding="utf-8")
                todo = root / "TODO.md"
                todo_text = "current:\ntask.md wl:2\n\nprevious:\n"
                todo.write_text(todo_text, encoding="utf-8")
                if case == "duplicate owner":
                    (root / "other.md").write_text(task_frontmatter(status="running", runat="wl:2") + "body\n", encoding="utf-8")
                args = StatusArgs(
                    root,
                    Path("task.md"),
                    "done",
                    "",
                    complete_live_no_mail=True,
                    active_target="wl:2",
                    manager_target="wl:1",
                    expected_task_sha256="0" * 64 if case == "task digest" else hashlib.sha256(text.encode()).hexdigest(),
                    expected_todo_sha256="0" * 64 if case == "todo digest" else hashlib.sha256(todo_text.encode()).hexdigest(),
                    expected_pane_id="%42",
                )
                original_task = task.read_text(encoding="utf-8")
                original_todo = todo.read_text(encoding="utf-8")
                pane_values = ["%41"] if case == "pane" else ["%42", "%42"]
                if case == "todo race":
                    def pane_with_todo_race(_target: str) -> str:
                        value = pane_values.pop(0)
                        if not pane_values:
                            todo.write_text(f"{todo_text}\nconcurrent\n", encoding="utf-8")
                        return value

                    pane_patch = patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane_with_todo_race)
                else:
                    pane_patch = patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane_values)
                ownership_patch = nullcontext()
                if case == "task race":
                    def owner_with_task_race(_root: Path, _target: str) -> tuple[Path, ...]:
                        task.write_text(f"{text}concurrent\n", encoding="utf-8")
                        return (task,)

                    ownership_patch = patch("omo_manager.omo_task_status.authoritative_active_target_task_paths", side_effect=owner_with_task_race)
                replace_patch = nullcontext()
                if case == "task write":
                    def fail_task_write(path: Path, payload: str, before: os.stat_result) -> None:
                        if path == task:
                            raise OSError("task write failed")
                        replace_if_unchanged_locked(path, payload, before)

                    replace_patch = patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_write)
                with pane_patch, ownership_patch, replace_patch, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                expected_task = f"{text}concurrent\n" if case == "task race" else original_task
                expected_todo = f"{todo_text}\nconcurrent\n" if case == "todo race" else original_todo
                self.assertEqual(expected_task, task.read_text(encoding="utf-8"))
                self.assertEqual(expected_todo, todo.read_text(encoding="utf-8"))

    def test_live_no_mail_rejects_todo_removed_before_strict_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original_task = task_frontmatter(status="running", runat="wl:2") + "body\n"
            updated_task = update_frontmatter_status(original_task, "done", "", root)
            task.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                complete_live_no_mail=True,
                active_target="wl:2",
                manager_target="wl:1",
                expected_task_sha256=hashlib.sha256(original_task.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                expected_pane_id="%42",
            )

            def remove_todo_then_finish(
                transaction_root: Path,
                transaction_task: Path,
                transaction_text: str,
                transaction_before: os.stat_result,
                *,
                locked: bool = False,
                todo_text: str | None = None,
                prepared_todo: str | None = None,
                todo_before: os.stat_result | None = None,
            ) -> None:
                todo.unlink()
                finish_done_transaction(
                    transaction_root,
                    transaction_task,
                    transaction_text,
                    transaction_before,
                    locked=locked,
                    todo_text=todo_text,
                    prepared_todo=prepared_todo,
                    todo_before=todo_before,
                )

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.finish_done_transaction", side_effect=remove_todo_then_finish),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            self.assertEqual(original_task, task.read_text(encoding="utf-8"))
            self.assertFalse(todo.exists())

            finish_done_transaction(root, task, updated_task, task.stat())
            self.assertEqual(updated_task, task.read_text(encoding="utf-8"))

    def test_active_task_tree_no_mail_parser_requires_exact_bindings(self) -> None:
        complete = [
            "--root", "/tmp/work_logs", "--close-active-task-tree-no-mail",
            "--shared-target", "agent_managers:5",
            "--protected-shared-task", "202608/mail_report_policy.md",
            "--protected-shared-sha256", "a" * 64,
            "--expected-task-sha256", "b" * 64,
            "--expected-todo-sha256", "c" * 64,
            "--expected-pane-id", "%3387",
            "--authority-file", "manager_mail/85c5dff58359-1298.txt",
            "--authority-lines", "3-4",
            "--authority-sha256", "d" * 64,
            "--no-mail-intent", ACTIVE_TASK_TREE_NO_MAIL_INTENT,
            "active_task_tree.md",
        ]
        args = parse_args(complete)
        self.assertTrue(args.close_active_task_tree_no_mail)
        self.assertEqual("done", args.status)
        self.assertEqual("agent_managers:5", args.shared_target)
        self.assertEqual(ACTIVE_TASK_TREE_NO_MAIL_INTENT, args.no_mail_intent)
        for option in ("--protected-shared-task", "--expected-task-sha256", "--expected-todo-sha256", "--expected-pane-id", "--authority-file", "--no-mail-intent"):
            candidate = complete.copy()
            index = candidate.index(option)
            del candidate[index : index + 2]
            with self.subTest(option=option), self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                parse_args(candidate)
        wrong_task = complete.copy()
        wrong_task[-1] = "other.md"
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(wrong_task)
        wrong_target = complete.copy()
        wrong_target[wrong_target.index("--shared-target") + 1] = "agent_managers:6"
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(wrong_target)

    def test_active_task_tree_no_mail_closes_metadata_only_and_preserves_shared_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, protected, todo, args, task_text, protected_text, todo_text = write_active_task_tree_fixture(root)
            output = io.StringIO()
            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3387") as pane,
                patch("omo_manager.omo_task_status.require_owner_completion") as email,
                patch("omo_manager.omo_task_status.stop_done_agent") as stop,
                patch("omo_manager.omo_task_status.record_close") as record,
                patch("omo_manager.omo_task_status.blocking_request") as blocking,
                redirect_stdout(output),
            ):
                self.assertEqual(0, run(args))
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("done", metadata.status)
            self.assertEqual("agent_managers:5", metadata.runat)
            self.assertEqual("hwl:3", metadata.managerat)
            self.assertFalse(metadata.is_manager)
            self.assertTrue(task.read_text(encoding="utf-8").endswith("completed evidence stays\n"))
            self.assertEqual(protected_text, protected.read_text(encoding="utf-8"))
            self.assertEqual(active_task_tree_todo_replacement(root, task, todo_text), todo.read_text(encoding="utf-8"))
            self.assertIn("without email or pane mutation", output.getvalue())
            self.assertEqual(3, pane.call_count)
            pane.assert_called_with("agent_managers:5")
            email.assert_not_called()
            stop.assert_not_called()
            record.assert_not_called()
            blocking.assert_not_called()

    def test_active_task_tree_no_mail_rejects_non_exact_bindings(self) -> None:
        for case in ("task digest", "todo digest", "protected digest", "protected TODO row", "authority digest", "authority text", "intent"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, protected, todo, args, task_text, protected_text, todo_text = write_active_task_tree_fixture(root)
                expected_todo = todo_text
                if case == "task digest":
                    args = replace(args, expected_task_sha256="0" * 64)
                elif case == "todo digest":
                    args = replace(args, expected_todo_sha256="0" * 64)
                elif case == "protected digest":
                    args = replace(args, protected_shared_sha256="0" * 64)
                elif case == "protected TODO row":
                    changed = todo_text.replace("active_task_tree.md agent_managers:5\n", "active_task_tree.md agent_managers:5\n202608/mail_report_policy.md agent_managers:5\n")
                    todo.write_text(changed, encoding="utf-8")
                    args = replace(args, expected_todo_sha256=hashlib.sha256(changed.encode()).hexdigest())
                    expected_todo = changed
                elif case == "authority digest":
                    args = replace(args, authority_sha256="0" * 64)
                elif case == "authority text":
                    authority = root / "manager_mail" / "85c5dff58359-1298.txt"
                    changed = authority.read_text(encoding="utf-8").replace("close it", "audit it")
                    authority.write_text(changed, encoding="utf-8")
                    args = replace(args, authority_sha256=hashlib.sha256(changed.encode()).hexdigest())
                elif case == "intent":
                    args = replace(args, no_mail_intent="generic-no-mail")
                with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3387"), redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(task_text, task.read_text(encoding="utf-8"))
                self.assertEqual(protected_text, protected.read_text(encoding="utf-8"))
                self.assertEqual(expected_todo, todo.read_text(encoding="utf-8"))

    def test_active_task_tree_no_mail_rechecks_drift_before_mutation(self) -> None:
        for case in ("source", "protected", "todo", "pane"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, protected, todo, args, task_text, protected_text, todo_text = write_active_task_tree_fixture(root)
                calls = 0

                def pane_with_drift(_target: str) -> str:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if case == "source":
                            task.write_text(f"{task_text}concurrent\n", encoding="utf-8")
                        elif case == "protected":
                            protected.write_text(f"{protected_text}concurrent\n", encoding="utf-8")
                        elif case == "todo":
                            todo.write_text(f"{todo_text}concurrent\n", encoding="utf-8")
                        elif case == "pane":
                            return "%9999"
                    return "%3387"

                with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane_with_drift), redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(f"{task_text}concurrent\n" if case == "source" else task_text, task.read_text(encoding="utf-8"))
                self.assertEqual(f"{protected_text}concurrent\n" if case == "protected" else protected_text, protected.read_text(encoding="utf-8"))
                self.assertEqual(f"{todo_text}concurrent\n" if case == "todo" else todo_text, todo.read_text(encoding="utf-8"))

    def test_active_task_tree_no_mail_refuses_rollback_after_committed_generation_replaced(self) -> None:
        for victim in ("task", "todo"):
            with self.subTest(victim=victim), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, protected, todo, args, task_text, protected_text, todo_text = write_active_task_tree_fixture(root)
                expected_task = update_frontmatter_status(task_text, "done", "", root)
                expected_todo = active_task_tree_todo_replacement(root, task, todo_text)
                calls = 0
                foreign: dict[str, os.stat_result] = {}

                def pane_with_post_commit_replacement(_target: str) -> str:
                    nonlocal calls
                    calls += 1
                    if calls == 3:
                        target = task if victim == "task" else todo
                        same_bytes = target.read_text(encoding="utf-8")
                        target.unlink()
                        target.write_text(same_bytes, encoding="utf-8")
                        foreign[victim] = target.stat()
                        return "%9999"
                    return "%3387"

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane_with_post_commit_replacement),
                    self.assertRaisesRegex(TaskFrontmatterError, "rollback refused"),
                ):
                    close_active_task_tree_no_mail(args, task, task_text, task.stat())
                self.assertEqual(expected_task, task.read_text(encoding="utf-8"))
                self.assertEqual(expected_todo, todo.read_text(encoding="utf-8"))
                self.assertEqual(protected_text, protected.read_text(encoding="utf-8"))
                self.assertEqual(foreign[victim].st_ino, (task if victim == "task" else todo).stat().st_ino)

    def test_done_live_close_is_idempotent_and_sends_no_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)
            state = {"live": True}
            capture_sha256 = "c" * 64

            def target_pane(_target: str) -> str:
                return "%42" if state["live"] else ""

            def start_ticks(_pid: int) -> int | None:
                return 73 if state["live"] else None

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                return ExitedCodexShell(args.expected_session_id, capture_sha256)

            def close(*values: object) -> None:
                identity = values[1]
                pre_close = values[10]
                assert callable(identity) and identity()
                assert callable(pre_close)
                pre_close()
                proof = Path(str(values[4]))
                audit = Path(str(values[5]))
                write_done_live_close_started(
                    proof, audit, str(values[6]), str(values[7]), str(values[12]),
                    args.active_target, args.expected_pane_id, args.expected_pane_pid, args.expected_pane_start_ticks,
                )
                state["live"] = False
                with (
                    patch("omo_manager.omo_codex_stop.pane_id", return_value=""),
                    patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=None),
                ):
                    promote_done_live_close_started(
                        proof, audit, str(values[7]), str(values[12]),
                        args.active_target, args.expected_pane_id, args.expected_pane_pid, args.expected_pane_start_ticks,
                    )

            output = io.StringIO()
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.process_start_ticks", side_effect=start_ticks),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=terminalize) as terminalize_call,
                patch("omo_manager.omo_task_status.validate_exited_codex_shell", return_value=capture_sha256),
                patch("omo_manager.omo_task_status.close_bound_tmux_target", side_effect=close) as guarded_close,
                patch("omo_manager.omo_task_status.require_owner_completion") as email,
                patch("omo_manager.omo_task_status.stop") as ordinary_stop,
                redirect_stdout(output),
            ):
                self.assertEqual(0, run(args))
                self.assertEqual(0, run(args))
            self.assertEqual(1, terminalize_call.call_count)
            self.assertEqual(1, guarded_close.call_count)
            self.assertEqual("done-live-no-mail-close", guarded_close.call_args.args[-2])
            self.assertRegex(guarded_close.call_args.args[-1], r"[0-9a-f]{64}\Z")
            email.assert_not_called()
            ordinary_stop.assert_not_called()
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            closed_text = task.read_text(encoding="utf-8")
            self.assertTrue(closed_text.startswith(text))
            self.assertEqual(1, closed_text.count("manager closed Codex agent"))
            audit = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual("complete", audit["state"])
            self.assertEqual(hashlib.sha256(closed_text.encode()).hexdigest(), audit["completed_task_sha256"])
            proof = args.audit_output.with_name(f".{args.audit_output.name}.owner-stopped")
            self.assertTrue(proof.is_file())
            self.assertIn("no email or task reopening", output.getvalue())

    def test_done_live_close_rejects_metadata_custody_owner_and_pane_drift(self) -> None:
        for case in ("status", "queue", "pending", "manager", "tool", "task digest", "todo", "todo digest", "owner", "pane", "process"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, text, todo, todo_text, args = self.write_done_live_close_case(root)
                if case == "status":
                    text = task_frontmatter(status="running", runat="wl:2") + "body\n"
                elif case == "queue":
                    text = task_frontmatter(status="done", runat="wl:2", pending_items=("unfinished",)) + "body\n"
                elif case == "pending":
                    text += "(pending)\nreport\n"
                elif case == "manager":
                    text = task_frontmatter(status="done", runat="wl:2", managerat="wl:9") + "body\n"
                elif case == "tool":
                    text = text.replace("tool: codex", "tool: cursor")
                if text != task.read_text(encoding="utf-8"):
                    task.write_text(text, encoding="utf-8")
                    args = replace(args, expected_task_sha256=hashlib.sha256(text.encode()).hexdigest())
                if case == "task digest":
                    args = replace(args, expected_task_sha256="0" * 64)
                if case == "todo":
                    todo_text = "current:\ntask.md wl:2\n\nprevious:\n"
                    todo.write_text(todo_text, encoding="utf-8")
                    args = replace(args, expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest())
                elif case == "todo digest":
                    args = replace(args, expected_todo_sha256="0" * 64)
                if case == "owner":
                    (root / "other.md").write_text(task_frontmatter(status="running", runat="wl:2") + "body\n", encoding="utf-8")
                target_pane = "%41" if case == "pane" else "%42"
                ticks = 74 if case == "process" else 73
                with (
                    patch("omo_manager.omo_task_status.park_target_pane_id", return_value=target_pane),
                    patch("omo_manager.omo_task_status.pane_id", return_value=target_pane),
                    patch("omo_manager.omo_task_status.process_start_ticks", return_value=ticks),
                    patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell") as terminalize,
                    patch("omo_manager.omo_task_status.close_bound_tmux_target") as close,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(args))
                terminalize.assert_not_called()
                close.assert_not_called()
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_done_live_close_requires_accepted_report_evidence_before_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.process_start_ticks", return_value=73),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=RuntimeError("accepted terminal report evidence is absent")) as terminalize,
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            terminalize.assert_called_once()
            close.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual("reserved", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_done_live_close_accepts_exact_manager_consumed_report_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)
            args = self.write_manager_consumed_receipt(root, task, args)

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                return ExitedCodexShell("11111111-2222-3333-4444-555555555555", "c" * 64)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.process_start_ticks", return_value=73),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell") as visible_terminalize,
                patch(
                    "omo_manager.omo_task_status.terminalize_bound_codex_to_shell_with_consumed_report",
                    side_effect=terminalize,
                ) as consumed_terminalize,
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            visible_terminalize.assert_not_called()
            consumed_terminalize.assert_called_once()
            close.assert_not_called()
            audit = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual("prepared", audit["state"])
            self.assertEqual(args.manager_consumed_report_receipt_sha256, audit["manager_consumed_receipt_sha256"])
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_manager_consumed_report_rejects_forgery_wrong_task_and_drift(self) -> None:
        for defect in ("forgery", "wrong task", "wrong manager", "drift", "replay"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task, _text, _todo, _todo_text, args = self.write_done_live_close_case(root)
                args = self.write_manager_consumed_receipt(root, task, args)
                receipt_path = args.manager_consumed_report_receipt
                assert receipt_path is not None
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if defect == "forgery":
                    receipt["accepted"] = False
                elif defect == "wrong task":
                    receipt["task"] = "other.md"
                elif defect == "wrong manager":
                    manager_task = Path(receipt["manager_acceptance"]["task"])
                    manager_task.write_text(
                        task_frontmatter(status="long_running", runat="wl:8", managerat="wl:9", is_manager=True),
                        encoding="utf-8",
                    )
                    prior = receipt["manager_acceptance"]["transaction"]
                    body = Path(prior["report"]).read_bytes()
                    transaction = self.write_report_transaction(
                        root / "private", manager_task, "wl:8", body, "other-manager-report"
                    )
                    receipt["manager_acceptance"] = {
                        "task": str(manager_task),
                        "task_sha256": hashlib.sha256(manager_task.read_bytes()).hexdigest(),
                        "target": "wl:8",
                        "transaction": transaction,
                    }
                else:
                    if defect == "drift":
                        Path(receipt["worker_report"]["report"]).write_text("changed\n", encoding="utf-8")
                        os.chmod(Path(receipt["worker_report"]["report"]), 0o600)
                    else:
                        args = replace(args, audit_output=(root / "private" / "replayed-audit.json").resolve())
                if defect in {"wrong task", "wrong manager"}:
                    receipt.pop("receipt_id")
                    receipt["receipt_id"] = hashlib.sha256(
                        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                if defect not in {"drift", "replay"}:
                    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                    os.chmod(receipt_path, 0o600)
                    args = replace(args, manager_consumed_report_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest())
                with self.assertRaises(TaskFrontmatterError):
                    validate_manager_consumed_report(args, task)

    def test_done_live_close_rejects_a_different_terminalized_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                return ExitedCodexShell("11111111-2222-3333-4444-555555555555", "c" * 64)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.process_start_ticks", return_value=73),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=terminalize),
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            close.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual("prepared", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_done_live_close_detects_concurrent_todo_change_before_terminal_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                todo.write_text(f"{todo_text}concurrent\n", encoding="utf-8")
                callback()
                raise AssertionError("unreachable")

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.process_start_ticks", return_value=73),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=terminalize),
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            close.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(f"{todo_text}concurrent\n", todo.read_text(encoding="utf-8"))
            self.assertEqual("prepared", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_done_live_close_rejects_terminal_capture_drift_before_guarded_close(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                return ExitedCodexShell(args.expected_session_id, "c" * 64)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.pane_id", return_value="%42"),
                patch("omo_manager.omo_task_status.process_start_ticks", return_value=73),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=terminalize),
                patch("omo_manager.omo_task_status.validate_exited_codex_shell", return_value="d" * 64),
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close,
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))
            close.assert_not_called()
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual("terminalized", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_done_live_close_recovers_after_close_before_audit_advance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, text, todo, todo_text, args = self.write_done_live_close_case(root)
            state = {"live": True}
            capture_sha256 = "d" * 64

            def target_pane(_target: str) -> str:
                return "%42" if state["live"] else ""

            def start_ticks(_pid: int) -> int | None:
                return 73 if state["live"] else None

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                return ExitedCodexShell(args.expected_session_id, capture_sha256)

            def close_then_interrupt(*values: object) -> None:
                pre_close = values[10]
                assert callable(pre_close)
                pre_close()
                write_done_live_close_started(
                    Path(str(values[4])),
                    Path(str(values[5])),
                    str(values[6]),
                    str(values[7]),
                    str(values[12]),
                    args.active_target,
                    args.expected_pane_id,
                    args.expected_pane_pid,
                    args.expected_pane_start_ticks,
                )
                state["live"] = False
                raise KeyboardInterrupt

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.process_start_ticks", side_effect=start_ticks),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=terminalize),
                patch("omo_manager.omo_task_status.validate_exited_codex_shell", return_value=capture_sha256),
                patch("omo_manager.omo_task_status.close_bound_tmux_target", side_effect=close_then_interrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                run(args)
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual("terminalized", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertTrue(done_live_close_started_path(args.audit_output).is_file())
            self.assertFalse(args.audit_output.with_name(f".{args.audit_output.name}.owner-stopped").exists())
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.process_start_ticks", side_effect=start_ticks),
                patch("omo_manager.omo_codex_stop.pane_id", return_value=""),
                patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=None),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell") as terminalize_again,
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close_again,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))
            terminalize_again.assert_not_called()
            close_again.assert_not_called()
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertEqual(1, task.read_text(encoding="utf-8").count("manager closed Codex agent"))
            self.assertEqual("complete", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_done_live_close_recovers_after_note_before_final_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, _text, _todo, _todo_text, args = self.write_done_live_close_case(root)
            state = {"live": True, "interrupted": False}
            capture_sha256 = "e" * 64
            original_replace = replace_private_audit

            def target_pane(_target: str) -> str:
                return "%42" if state["live"] else ""

            def start_ticks(_pid: int) -> int | None:
                return 73 if state["live"] else None

            def terminalize(*values: object) -> ExitedCodexShell:
                callback = values[6]
                assert callable(callback)
                callback()
                return ExitedCodexShell(args.expected_session_id, capture_sha256)

            def close(*values: object) -> None:
                pre_close = values[10]
                assert callable(pre_close)
                pre_close()
                proof = Path(str(values[4]))
                audit = Path(str(values[5]))
                write_done_live_close_started(
                    proof, audit, str(values[6]), str(values[7]), str(values[12]),
                    args.active_target, args.expected_pane_id, args.expected_pane_pid, args.expected_pane_start_ticks,
                )
                state["live"] = False
                with (
                    patch("omo_manager.omo_codex_stop.pane_id", return_value=""),
                    patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=None),
                ):
                    promote_done_live_close_started(
                        proof, audit, str(values[7]), str(values[12]),
                        args.active_target, args.expected_pane_id, args.expected_pane_pid, args.expected_pane_start_ticks,
                    )

            def interrupt_final_audit(path: Path, expected: str, updated: str) -> None:
                if json.loads(updated)["state"] == "complete" and not state["interrupted"]:
                    state["interrupted"] = True
                    raise KeyboardInterrupt
                original_replace(path, expected, updated)

            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.process_start_ticks", side_effect=start_ticks),
                patch("omo_manager.omo_task_status.terminalize_bound_codex_to_shell", side_effect=terminalize),
                patch("omo_manager.omo_task_status.validate_exited_codex_shell", return_value=capture_sha256),
                patch("omo_manager.omo_task_status.close_bound_tmux_target", side_effect=close),
                patch("omo_manager.omo_task_status.replace_private_audit", side_effect=interrupt_final_audit),
                self.assertRaises(KeyboardInterrupt),
            ):
                run(args)
            self.assertEqual("note-prepared", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual(1, task.read_text(encoding="utf-8").count("manager closed Codex agent"))
            with (
                patch("omo_manager.omo_task_status.park_target_pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.pane_id", side_effect=target_pane),
                patch("omo_manager.omo_task_status.process_start_ticks", side_effect=start_ticks),
                patch("omo_manager.omo_task_status.close_bound_tmux_target") as close_again,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))
            close_again.assert_not_called()
            self.assertEqual(1, task.read_text(encoding="utf-8").count("manager closed Codex agent"))
            self.assertEqual("complete", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_normal_done_refuses_human_target_before_state_or_tmux_change_without_compatible_close_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter(status="running", runat="hwork:1") + "body\n"
            task.write_text(original, encoding="utf-8")
            args = StatusArgs(root, Path("task.md"), "done", "")
            with patch("omo_manager.omo_task_status.exact_pane_id") as pane_id, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))
            pane_id.assert_not_called()
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_normal_done_forwards_human_authority_to_close_preflights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            text = task_frontmatter(status="running", runat="hwork:1") + "body\n"
            task.write_text(text, encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                human_close_authorization_source="manager_mail/authority.txt",
                human_close_authorization_sha256="a" * 64,
            )
            record_args = StopArgs("hwork:1", 0.0, 10, False, False, root, "task.md", True, 0.0)
            with (
                patch("omo_manager.omo_task_status.human_close_stop_args", return_value=record_args) as build,
                patch("omo_manager.omo_task_status.validate_human_close_authorization") as validate,
                patch("omo_manager.omo_task_status.stop_done_agent", return_value=(record_args, "")) as close,
            ):
                self.assertEqual(0, run(args))
            self.assertEqual(1, build.call_count)
            validate.assert_called_once_with(record_args)
            close.assert_called_once_with(root, task, parse_task_metadata(text, root), "manager_mail/authority.txt", "a" * 64)

    def test_low_priority_current_normalization_promotes_only_one_exact_active_manager_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "manager.md"
            text = task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1", is_manager=True, pending_items=("keep ordered queue",)) + "body\n"
            path.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nother.md vl:9\n\nlow priority:\nmanager.md vl:2\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "", "", normalize_low_priority_current=True, active_target="vl:2", manager_target="vl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())

            self.assertEqual(0, run(args))
            self.assertEqual(text, path.read_text(encoding="utf-8"))
            self.assertEqual("current:\nmanager.md vl:2\nother.md vl:9\n\nlow priority:\n\nhuman pending:\n\nprevious:\n", todo.read_text(encoding="utf-8"))

    def test_low_priority_current_normalization_rejects_adversarial_task_and_todo_drift(self) -> None:
        cases = (
            ("digest", task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1", is_manager=True), "low-priority normalization source bytes"),
            ("status", task_frontmatter(status="blocked", blocked_on="human", runat="vl:2", managerat="vl:1", is_manager=True), "unchanged active v1 manager"),
            ("target", task_frontmatter(status="long_running", runat="vl:3", managerat="vl:1", is_manager=True), "unchanged active v1 manager"),
            ("manager", task_frontmatter(status="long_running", runat="vl:2", managerat="vl:3", is_manager=True), "unchanged active v1 manager"),
            ("worker", task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1"), "unchanged active v1 manager"),
            ("pending", task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1", is_manager=True) + "(pending)\n", "unchanged active v1 manager"),
        )
        for label, text, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "manager.md"
                path.write_text(text, encoding="utf-8")
                (root / "TODO.md").write_text("current:\n\nlow priority:\nmanager.md vl:2\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")
                digest = "0" * 64 if label == "digest" else hashlib.sha256(text.encode()).hexdigest()
                args = StatusArgs(root, Path("manager.md"), "", "", normalize_low_priority_current=True, active_target="vl:2", manager_target="vl:1", source_sha256=digest)
                with self.assertRaisesRegex(TaskFrontmatterError, expected):
                    normalize_low_priority_current(args, path, text, path.stat())

        todo_cases = (
            ("missing", "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\n", "exactly one TODO row in low priority"),
            ("duplicate", "current:\n\nlow priority:\nmanager.md vl:2\nmanager.md vl:2\n\nhuman pending:\n\nprevious:\n", "exactly one TODO row in low priority"),
            ("wrong target", "current:\n\nlow priority:\nmanager.md vl:3\n\nhuman pending:\n\nprevious:\n", "exact active target"),
            ("annotation", "current:\n\nlow priority:\nmanager.md vl:2 reviewer annotation\n\nhuman pending:\n\nprevious:\n", "sole canonical TODO row"),
            ("backticks", "current:\n\nlow priority:\n`manager.md` vl:2\n\nhuman pending:\n\nprevious:\n", "sole canonical TODO row"),
            ("whitespace", "current:\n\nlow priority:\n  manager.md vl:2  \n\nhuman pending:\n\nprevious:\n", "sole canonical TODO row"),
            ("wrong section", "current:\nmanager.md vl:2\n\nlow priority:\n\nhuman pending:\n\nprevious:\n", "exactly one TODO row in low priority"),
            ("human pending", "current:\n\nlow priority:\nmanager.md vl:2\n\nhuman pending:\nmanager.md vl:2\n\nprevious:\n", "exactly one TODO row in low priority"),
            ("previous", "current:\n\nlow priority:\nmanager.md vl:2\n\nhuman pending:\n\nprevious:\nmanager.md vl:2\n", "exactly one TODO row in low priority"),
        )
        for label, todo_text, expected in todo_cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "manager.md"
                text = task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1", is_manager=True)
                path.write_text(text, encoding="utf-8")
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                args = StatusArgs(root, Path("manager.md"), "", "", normalize_low_priority_current=True, active_target="vl:2", manager_target="vl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
                with self.assertRaisesRegex(TaskFrontmatterError, expected):
                    normalize_low_priority_current(args, path, text, path.stat())

    def test_low_priority_current_normalization_rechecks_locked_ownership_and_rejects_human_or_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "manager.md"
            text = task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1", is_manager=True)
            path.write_text(text, encoding="utf-8")
            (root / "other.md").write_text(task_frontmatter(status="running", runat="vl:2", managerat="vl:1") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\n\nlow priority:\nmanager.md vl:2\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "", "", normalize_low_priority_current=True, active_target="vl:2", manager_target="vl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "sole active owner"):
                normalize_low_priority_current(args, path, text, path.stat())

            human_args = StatusArgs(root, Path("manager.md"), "", "", normalize_low_priority_current=True, active_target="h:2", manager_target="vl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "non-human exact"):
                normalize_low_priority_current(human_args, path, text, path.stat())
            v2 = root / "v2.md"
            v2_text = v2_task().replace("runat: wl:2", "runat: vl:2").replace("managerat: wl:1", "managerat: vl:1").replace("is_manager: false", "is_manager: true")
            v2.write_text(v2_text, encoding="utf-8")
            v2_args = StatusArgs(root, Path("v2.md"), "", "", normalize_low_priority_current=True, active_target="vl:2", manager_target="vl:1", source_sha256=hashlib.sha256(v2_text.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "unchanged active v1 manager"):
                normalize_low_priority_current(v2_args, v2, v2_text, v2.stat())

    def test_parse_low_priority_current_requires_exact_targets_and_digest(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--root", "/tmp/work", "--normalize-low-priority-current", "manager.md"])
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--root", "/tmp/work", "--active-target", "vl:2", "manager.md", "running"])
        args = parse_args(["--root", "/tmp/work", "--normalize-low-priority-current", "--active-target", "vl:2", "--manager-target", "vl:1", "--source-sha256", "a" * 64, "manager.md"])
        self.assertTrue(args.normalize_low_priority_current)
        self.assertEqual(("vl:2", "vl:1"), (args.active_target, args.manager_target))

    def test_low_priority_current_normalization_rejects_task_or_todo_race_and_keeps_lock_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "manager.md"
            text = task_frontmatter(status="long_running", runat="vl:2", managerat="vl:1", is_manager=True)
            path.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\n\nlow priority:\nmanager.md vl:2\n\nhuman pending:\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "", "", normalize_low_priority_current=True, active_target="vl:2", manager_target="vl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
            before = path.stat()
            path.write_text(text + "drift\n", encoding="utf-8")
            with self.assertRaisesRegex(TaskFrontmatterError, "source bytes do not match"):
                normalize_low_priority_current(args, path, text, before)

            path.write_text(text, encoding="utf-8")
            def mutate_todo(*_args: object) -> str:
                todo.write_text(todo_text.replace("vl:2", "vl:3"), encoding="utf-8")
                return "current:\nmanager.md vl:2\n\nlow priority:\n\nhuman pending:\n\nprevious:\n"
            with patch("omo_manager.omo_task_status.reconcile_todo_text", side_effect=mutate_todo):
                with self.assertRaisesRegex(TaskFrontmatterError, "TODO changed while low-priority normalization"):
                    normalize_low_priority_current(args, path, text, path.stat())
            self.assertEqual(todo_text.replace("vl:2", "vl:3"), todo.read_text(encoding="utf-8"))

            todo.write_text(todo_text, encoding="utf-8")
            locks: list[str] = []
            def record_lock(candidate: Path):
                locks.append(candidate.name)
                return nullcontext()
            with patch("omo_manager.omo_task_status.root_membership_lock", side_effect=lambda _root: nullcontext()), patch("omo_manager.omo_task_status.task_target_lock", side_effect=lambda _root, _target: nullcontext()), patch("omo_manager.omo_task_status.task_file_lock", side_effect=record_lock):
                normalize_low_priority_current(args, path, text, path.stat())
            self.assertEqual(sorted(locks), locks)
    def test_retired_todo_normalization_is_index_only_and_unblocks_proven_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "legacy.md"
            historical = task_frontmatter(status="running", runat="wl:2") + "body\n"
            task.write_text(historical, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "legacy.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            current = task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n(manager closed Codex agent; tmux target `wl:2`.)\n"
            task.write_text(current, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\n\nhuman pending:\nlegacy.md\n\nprevious:\n", encoding="utf-8")
            source_sha256 = hashlib.sha256(current.encode()).hexdigest()
            normalize_args = StatusArgs(root, Path("legacy.md"), "", "", normalize_retired_todo=True, source_sha256=source_sha256)
            tmux_names = ("stop", "capture", "exact_pane_id", "pane_id", "close_note", "record_close", "close_exited_codex_shell", "blocking_request")
            with patch.multiple("omo_manager.omo_task_status", **{name: DEFAULT for name in tmux_names}) as mocked:
                self.assertEqual(0, run(normalize_args))
                for mock in mocked.values():
                    mock.assert_not_called()
            self.assertEqual(current, task.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nhuman pending:\nlegacy.md retired\n\nprevious:\n", todo.read_text(encoding="utf-8"))

            close_args = StatusArgs(root, Path("legacy.md"), "done", "", close_retired_done=True, historical_target="wl:2", historical_commit=commit, source_sha256=source_sha256)
            with patch.multiple("omo_manager.omo_task_status", **{name: DEFAULT for name in tmux_names}) as mocked:
                self.assertEqual(0, run(close_args))
                for mock in mocked.values():
                    mock.assert_not_called()
            self.assertIn("status: done\n", task.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nhuman pending:\n\nprevious:\nlegacy.md wl:2\n", todo.read_text(encoding="utf-8"))

    def test_retired_todo_normalization_rejects_drift_queue_and_noncanonical_row(self) -> None:
        for label, task_text, todo_text, source_sha256, expected in (
            ("digest", task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n", "human pending:\nlegacy.md\n", "0" * 64, "source bytes"),
            ("queue", task_frontmatter(status="blocked", blocked_on="human", runat="retired", pending_items=("open",)) + "body\n", "human pending:\nlegacy.md\n", "", "empty queue"),
            ("v2", v2_task().replace("runat: wl:2", "runat: retired"), "human pending:\nlegacy.md\n", "", "unchanged v1"),
            ("manager", task_frontmatter(status="blocked", blocked_on="human", runat="retired", is_manager=True) + "body\n", "human pending:\nlegacy.md\n", "", "non-manager"),
            ("pending marker", task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "(pending)\n", "human pending:\nlegacy.md\n", "", "no live pending marker"),
            ("row", task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n", "human pending:\nlegacy.md stale\n", "", "exact targetless task row"),
            ("target suffix", task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n", "human pending:\nlegacy.md wl:2\n", "", "exact targetless task row"),
            ("duplicate", task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n", "current:\nlegacy.md\n\nhuman pending:\nlegacy.md\n", "", "exactly one human-pending TODO row"),
            ("outside human pending", task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n", "current:\nlegacy.md\n\nhuman pending:\n", "", "exactly one human-pending TODO row"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "legacy.md"
                task.write_text(task_text, encoding="utf-8")
                todo = root / "TODO.md"
                todo.write_text(todo_text, encoding="utf-8")
                args = StatusArgs(root, Path("legacy.md"), "", "", normalize_retired_todo=True, source_sha256=source_sha256 or hashlib.sha256(task_text.encode()).hexdigest())
                with self.assertRaisesRegex(TaskFrontmatterError, expected):
                    normalize_retired_todo(args, task, task_text, task.stat())
                self.assertEqual(task_text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_retired_todo_normalization_parser_rejects_unrelated_lifecycle_inputs(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args([
                "--normalize-retired-todo",
                "--source-sha256", "a" * 64,
                "--historical-target", "wl:2",
                "legacy.md",
            ])
        args = parse_args([
            "--normalize-retired-todo",
            "--source-sha256", "a" * 64,
            "legacy.md",
        ])
        self.assertTrue(args.normalize_retired_todo)
        self.assertEqual("", args.status)
        self.assertEqual("a" * 64, args.source_sha256)

    def test_retired_closure_is_metadata_only_with_git_proof_and_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "retired_legacy.md"
            historical = task_frontmatter(status="running", runat="wl:2") + "body\n"
            task.write_text(historical, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "retired_legacy.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            current = task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n(manager closed Codex agent after completed work; tmux target `wl:2`; queue preserved.)\n"
            task.write_text(current, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\n\nhuman pending:\nretired_legacy.md retired\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("retired_legacy.md"), "done", "", close_retired_done=True, historical_target="wl:2", historical_commit=commit, source_sha256=hashlib.sha256(current.encode()).hexdigest())
            tmux_names = ("stop", "capture", "exact_pane_id", "pane_id", "close_note", "record_close", "close_exited_codex_shell", "blocking_request")
            output = io.StringIO()
            with patch.multiple("omo_manager.omo_task_status", **{name: DEFAULT for name in tmux_names}) as mocked, redirect_stdout(output):
                self.assertEqual(0, run(args))
                for mock in mocked.values():
                    mock.assert_not_called()
            self.assertIn("no pane was signalled", output.getvalue())
            self.assertNotIn("Closed wl:2", output.getvalue())
            self.assertIn("status: done\n", task.read_text(encoding="utf-8"))
            self.assertIn("runat: wl:2\n", task.read_text(encoding="utf-8"))
            self.assertNotIn("blocked_on:", task.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nhuman pending:\n\nprevious:\nretired_legacy.md wl:2\n", todo.read_text(encoding="utf-8"))

    def test_retired_closure_fails_closed_without_digest_close_note_or_git_target(self) -> None:
        for label, digest, note, target, expected in (
            ("digest", "0" * 64, "(manager closed Codex agent; tmux target `wl:2`.)\n", "wl:2", "source bytes"),
            ("note", "", "", "wl:2", "manager-close note"),
            ("target", "", "(manager closed Codex agent; tmux target `wl:9`.)\n", "wl:9", "historical Git blob"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "legacy.md"
                historical = task_frontmatter(status="running", runat="wl:2") + "body\n"
                task.write_text(historical, encoding="utf-8")
                subprocess.run(["git", "init", "-q", str(root)], check=True)
                subprocess.run(["git", "-C", str(root), "add", "legacy.md"], check=True)
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
                commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                current = task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n" + note
                task.write_text(current, encoding="utf-8")
                (root / "TODO.md").write_text("current:\n\nhuman pending:\nlegacy.md retired\n\nprevious:\n", encoding="utf-8")
                args = StatusArgs(root, Path("legacy.md"), "done", "", close_retired_done=True, historical_target=target, historical_commit=commit, source_sha256=digest or hashlib.sha256(current.encode()).hexdigest())
                with self.assertRaisesRegex(TaskFrontmatterError, expected):
                    close_retired_done(args, task, current, task.stat())
                self.assertEqual(current, task.read_text(encoding="utf-8"))

    def test_retired_closure_rejects_noncanonical_todo_and_reports_rollback_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "legacy.md"
            historical = task_frontmatter(status="running", runat="wl:2") + "body\n"
            task.write_text(historical, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "legacy.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            current = task_frontmatter(status="blocked", blocked_on="human", runat="retired") + "body\n(manager closed Codex agent; tmux target `wl:2`.)\n"
            task.write_text(current, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\n\nhuman pending:\nlegacy.md carried-forward-retired\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("legacy.md"), "done", "", close_retired_done=True, historical_target="wl:2", historical_commit=commit, source_sha256=hashlib.sha256(current.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "targetless retired TODO row"):
                close_retired_done(args, task, current, task.stat())
            todo.write_text("current:\n\nhuman pending:\nlegacy.md retired\n\nprevious:\n", encoding="utf-8")
            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=[None, OSError("TODO failed"), OSError("rollback failed")]):
                with self.assertRaisesRegex(TaskFrontmatterError, "task rollback also failed: rollback failed"):
                    close_retired_done(args, task, current, task.stat())

    def test_retired_closure_recovers_done_task_before_todo_intermediate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "legacy.md"
            historical = task_frontmatter(status="running", runat="wl:2") + "body\n"
            task.write_text(historical, encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "legacy.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            intermediate = task_frontmatter(status="done", runat="wl:2") + "body\n(manager closed Codex agent; tmux target `wl:2`.)\n"
            task.write_text(intermediate, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\n\nhuman pending:\nlegacy.md retired\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("legacy.md"), "done", "", close_retired_done=True, historical_target="wl:2", historical_commit=commit, source_sha256=hashlib.sha256(intermediate.encode()).hexdigest())

            self.assertEqual("wl:2", close_retired_done(args, task, intermediate, task.stat()))

            self.assertEqual(intermediate, task.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nhuman pending:\n\nprevious:\nlegacy.md wl:2\n", todo.read_text(encoding="utf-8"))

    def test_retired_closure_parser_rejects_unused_task_digest(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args([
                "--root", "/tmp",
                "--close-retired-done",
                "--historical-target", "wl:2",
                "--historical-commit", "a" * 40,
                "--source-sha256", "b" * 64,
                "--task-sha256", "c" * 64,
                "legacy.md",
            ])

    def test_shared_target_closure_is_metadata_only_and_moves_current_to_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            original = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True) + "body\n"
            task.write_text(original, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nmanager.md wl:1\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(original.encode()).hexdigest())
            tmux_names = (
                "stop", "capture", "exact_pane_id", "pane_id", "close_note", "record_close",
                "close_exited_codex_shell", "blocking_request",
            )
            with patch.multiple("omo_manager.omo_task_status", **{name: DEFAULT for name in tmux_names}) as mocked:
                self.assertEqual(0, run(args))
                for name, mock in mocked.items():
                    mock.assert_not_called()
            self.assertIn("status: done\n", task.read_text(encoding="utf-8"))
            self.assertIn("runat: wl:1\n", task.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nprevious:\nmanager.md wl:1\n", todo.read_text(encoding="utf-8"))

    def test_shared_target_closure_fails_closed_for_digest_queue_ownership_and_todo_drift(self) -> None:
        cases = (
            ("digest", lambda text, todo: StatusArgs(Path("."), Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256="0" * 64), "source bytes"),
            ("queue", lambda text, todo: StatusArgs(Path("."), Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest()), "empty queue"),
            ("todo", lambda text, todo: StatusArgs(Path("."), Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest()), "TODO"),
        )
        for label, make_args, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "manager.md"
                text = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True, pending_items=("open",)) if label == "queue" else task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True)
                task.write_text(text, encoding="utf-8")
                todo_text = "current:\nother.md wl:1\n\nprevious:\n" if label == "todo" else "current:\nmanager.md wl:1\n\nprevious:\n"
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                args = make_args(text, todo_text)
                args = StatusArgs(root, args.task_file, args.status, args.blocked_on, close_shared_target=True, shared_target=args.shared_target, source_sha256=args.source_sha256)
                with self.assertRaisesRegex(TaskFrontmatterError, expected):
                    finish_shared_target_done(args, task, text, task.stat())
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, (root / "TODO.md").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            text = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True)
            task.write_text(text, encoding="utf-8")
            (root / "other.md").write_text(task_frontmatter(status="running", runat="wl:1", managerat="vl:1") + "other\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nmanager.md wl:1\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "sole active owner"):
                finish_shared_target_done(args, task, text, task.stat())

    def test_shared_target_closure_rejects_malformed_todo_sections_rows_and_target(self) -> None:
        cases = (
            ("wrong section", "human pending:\nmanager.md wl:1\n\nprevious:\n", "current"),
            ("duplicate current", "current:\nmanager.md wl:1\n\ncurrent:\n\nprevious:\n", "exactly one canonical"),
            ("duplicate", "current:\nmanager.md wl:1\nmanager.md wl:1\n\nprevious:\n", "exactly one"),
            ("wrong target", "current:\nmanager.md vl:2\n\nprevious:\n", "exact shared target"),
            ("missing previous", "current:\nmanager.md wl:1\n", "previous"),
            ("mixed-case current", "Current:\nmanager.md wl:1\n\nprevious:\n", "canonical lowercase"),
            ("mixed-case previous", "current:\nmanager.md wl:1\n\nPrevious:\n", "canonical lowercase"),
        )
        for label, todo_text, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "manager.md"
                text = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True)
                task.write_text(text, encoding="utf-8")
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                args = StatusArgs(root, Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
                with self.assertRaisesRegex(TaskFrontmatterError, expected):
                    finish_shared_target_done(args, task, text, task.stat())
                self.assertEqual(text, task.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, (root / "TODO.md").read_text(encoding="utf-8"))

    def test_shared_target_closure_rolls_back_todo_when_task_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            text = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True)
            task.write_text(text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\nmanager.md wl:1\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())

            def fail_task_replace(target: Path, replacement: str, before: os.stat_result) -> None:
                if target == task:
                    raise OSError("task write failed")
                replace_if_unchanged_locked(target, replacement, before)

            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_replace):
                with self.assertRaises(OSError):
                    finish_shared_target_done(args, task, text, task.stat())
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_shared_target_closure_rejects_todo_changed_after_strict_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            original = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True) + "body\n"
            task.write_text(original, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\nmanager.md wl:1\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            args = StatusArgs(
                root,
                Path("manager.md"),
                "done",
                "",
                close_shared_target=True,
                shared_target="wl:1",
                source_sha256=hashlib.sha256(original.encode()).hexdigest(),
            )
            original_update = update_frontmatter_status

            def inject_todo_change(current: str, status: str, blocked_on: str, root_arg: Path) -> str:
                todo.write_text("current:\nmanager.md wl:9\n\nprevious:\n", encoding="utf-8")
                return original_update(current, status, blocked_on, root_arg)

            with patch("omo_manager.omo_task_status.update_frontmatter_status", side_effect=inject_todo_change):
                with self.assertRaisesRegex(TaskFrontmatterError, "TODO changed"):
                    finish_shared_target_done(args, task, original, task.stat())
            self.assertEqual(original, task.read_text(encoding="utf-8"))
            self.assertEqual("current:\nmanager.md wl:9\n\nprevious:\n", todo.read_text(encoding="utf-8"))

    def test_shared_target_closure_moves_backticked_current_row_with_strict_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            original = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True) + "body\n"
            task.write_text(original, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\n`manager.md` wl:1\n\nprevious:\n", encoding="utf-8")
            args = StatusArgs(
                root,
                Path("manager.md"),
                "done",
                "",
                close_shared_target=True,
                shared_target="wl:1",
                source_sha256=hashlib.sha256(original.encode()).hexdigest(),
            )
            self.assertEqual(0, run(args))
            self.assertIn("status: done\n", task.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nprevious:\n`manager.md` wl:1\n", todo.read_text(encoding="utf-8"))

    def test_shared_target_closure_rechecks_manager_children_under_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            text = task_frontmatter(status="long_running", runat="wl:1", managerat="vl:1", is_manager=True)
            task.write_text(text, encoding="utf-8")
            child = root / "child.md"
            child.write_text(task_frontmatter(status="running", runat="vl:2", managerat="wl:1") + "child\n", encoding="utf-8")
            todo_text = "current:\nmanager.md wl:1\n\nprevious:\n"
            (root / "TODO.md").write_text(todo_text, encoding="utf-8")
            args = StatusArgs(root, Path("manager.md"), "done", "", close_shared_target=True, shared_target="wl:1", source_sha256=hashlib.sha256(text.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "active child"):
                finish_shared_target_done(args, task, text, task.stat())
            self.assertEqual(text, task.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, (root / "TODO.md").read_text(encoding="utf-8"))

    def test_shared_target_cancellation_clears_only_memory_and_never_accesses_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory_research_mgr.md"
            original = task_frontmatter(
                status="blocked", blocked_on="paused", pending_items=("one", "two"),
                runat="wl:32", managerat="wl:30", is_manager=True,
            ) + "history\n"
            memory.write_text(original, encoding="utf-8")
            protected = root / "transcription_sw.md"
            protected_text = task_frontmatter(
                status="blocked", blocked_on="human", pending_items=("keep",),
                runat="wl:32", managerat="wl:1",
            ) + "transcription evidence\n"
            protected.write_text(protected_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntranscription_sw.md wl:32\n\nhuman pending:\nmemory_research_mgr.md wl:32\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            mail = root / "manager_mail"
            mail.mkdir(mode=0o700)
            authority_text = "Subject: Re: Authorize memory_research_mgr.md relocation\n\nClose the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            authority = mail / "85c5dff58359-1290.txt"
            authority.write_text(authority_text, encoding="utf-8")
            authority.chmod(0o600)
            envelope = root / "authority.md"
            excerpt = "Close the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            envelope_text = (
                '<human_instruction authoritative="true" source="manager_mail/85c5dff58359-1290.txt:3-4">\n'
                f"{excerpt}</human_instruction>\n"
            )
            envelope.write_text(envelope_text, encoding="utf-8")
            private = root / "private"
            private.mkdir(mode=0o700)
            args = StatusArgs(
                root, Path("memory_research_mgr.md"), "done", "",
                cancel_shared_target=True,
                shared_target="wl:32",
                protected_shared_task=Path("transcription_sw.md"),
                protected_shared_sha256=hashlib.sha256(protected_text.encode()).hexdigest(),
                source_sha256=hashlib.sha256(original.encode()).hexdigest(),
                expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                authority_file=Path("manager_mail/85c5dff58359-1290.txt"),
                authority_lines=(3, 4),
                authority_sha256=hashlib.sha256(authority_text.encode()).hexdigest(),
                authority_envelope=Path("authority.md"),
                authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
                audit_output=private / "cancel.yaml",
            )
            tmux_names = (
                "stop", "capture", "exact_pane_id", "pane_id", "close_note", "record_close",
                "close_exited_codex_shell", "blocking_request", "tmux",
            )
            with patch.multiple("omo_manager.omo_task_status", **{name: DEFAULT for name in tmux_names}) as mocked:
                self.assertEqual("wl:32", cancel_shared_target_done(args, memory, original, memory.stat()))
                for mock in mocked.values():
                    mock.assert_not_called()
            result = parse_task_metadata(memory.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual("done", result.status)
            self.assertEqual((), result.pending_task_items)
            self.assertEqual(protected_text, protected.read_text(encoding="utf-8"))
            self.assertEqual(
                "current:\ntranscription_sw.md wl:32\n\nhuman pending:\n\nprevious:\nmemory_research_mgr.md wl:32\n",
                todo.read_text(encoding="utf-8"),
            )
            audit = (private / "cancel.yaml").read_text(encoding="utf-8")
            self.assertIn("final-result: success", audit)
            self.assertIn("- one\n- two\n", audit)

    def test_shared_target_cancellation_rejects_authority_protected_and_owner_drift(self) -> None:
        for label in ("authority", "alternate-source", "protected", "protected-row", "owner", "nested"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task_dir = root / "nested" if label == "nested" else root
                task_dir.mkdir(exist_ok=True)
                memory = task_dir / "memory_research_mgr.md"
                original = task_frontmatter(status="blocked", blocked_on="paused", pending_items=("one",), runat="wl:32", is_manager=True)
                memory.write_text(original, encoding="utf-8")
                protected = task_dir / "transcription_sw.md"
                protected_text = task_frontmatter(status="blocked", blocked_on="human", pending_items=("keep",), runat="wl:32")
                protected.write_text(protected_text, encoding="utf-8")
                if label == "owner":
                    (root / "third.md").write_text(task_frontmatter(status="running", runat="wl:32"), encoding="utf-8")
                todo = root / "TODO.md"
                todo_text = "current:\ntranscription_sw.md wl:32\n\nhuman pending:\nmemory_research_mgr.md wl:32\n\nprevious:\n"
                if label == "protected-row":
                    todo_text = "current:\ntranscription_sw.md wl:32\ntranscription_sw.md wl:32\n\nhuman pending:\nmemory_research_mgr.md wl:32\n\nprevious:\n"
                todo.write_text(todo_text, encoding="utf-8")
                mail = root / "manager_mail"
                mail.mkdir(mode=0o700)
                authority_text = "Subject: Re: Authorize memory_research_mgr.md relocation\n\nClose the wrong thing.\nOther question\n" if label == "authority" else "Subject: Re: Authorize memory_research_mgr.md relocation\n\nClose the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
                authority_name = "alternate.txt" if label == "alternate-source" else "85c5dff58359-1290.txt"
                authority = mail / authority_name
                authority.write_text(authority_text, encoding="utf-8")
                authority.chmod(0o600)
                envelope = root / "authority.md"
                locator = f"manager_mail/{authority_name}:3-4"
                excerpt = "\n".join(authority_text.splitlines()[2:4]) + "\n"
                envelope_text = f'<human_instruction authoritative="true" source="{locator}">\n' + excerpt + "</human_instruction>\n"
                envelope.write_text(envelope_text, encoding="utf-8")
                private = root / "private"
                private.mkdir(mode=0o700)
                args = StatusArgs(
                    root, memory.relative_to(root), "done", "", cancel_shared_target=True,
                    shared_target="wl:32", protected_shared_task=protected.relative_to(root),
                    protected_shared_sha256=("0" * 64 if label == "protected" else hashlib.sha256(protected_text.encode()).hexdigest()),
                    source_sha256=hashlib.sha256(original.encode()).hexdigest(),
                    expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                    authority_file=Path("manager_mail") / authority_name, authority_lines=(3, 4),
                    authority_sha256=hashlib.sha256(authority_text.encode()).hexdigest(),
                    authority_envelope=Path("authority.md"), authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(),
                    audit_output=private / "cancel.yaml",
                )
                with self.assertRaises(TaskFrontmatterError):
                    cancel_shared_target_done(args, memory, original, memory.stat())
                self.assertEqual(original, memory.read_text(encoding="utf-8"))
                self.assertEqual(protected_text, protected.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_shared_target_cancellation_rejects_authority_drift_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory_research_mgr.md"
            original = task_frontmatter(status="blocked", blocked_on="paused", pending_items=("one",), runat="wl:32", is_manager=True)
            memory.write_text(original, encoding="utf-8")
            protected = root / "transcription_sw.md"
            protected_text = task_frontmatter(status="blocked", blocked_on="human", pending_items=("keep",), runat="wl:32")
            protected.write_text(protected_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntranscription_sw.md wl:32\n\nhuman pending:\nmemory_research_mgr.md wl:32\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            mail = root / "manager_mail"
            mail.mkdir(mode=0o700)
            authority_text = "Subject: Re: Authorize memory_research_mgr.md relocation\n\nClose the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            authority = mail / "85c5dff58359-1290.txt"
            authority.write_text(authority_text, encoding="utf-8")
            authority.chmod(0o600)
            envelope = root / "authority.md"
            excerpt = "Close the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            envelope_text = '<human_instruction authoritative="true" source="manager_mail/85c5dff58359-1290.txt:3-4">\n' + excerpt + "</human_instruction>\n"
            envelope.write_text(envelope_text, encoding="utf-8")
            private = root / "private"
            private.mkdir(mode=0o700)
            args = StatusArgs(
                root, Path("memory_research_mgr.md"), "done", "", cancel_shared_target=True,
                shared_target="wl:32", protected_shared_task=Path("transcription_sw.md"),
                protected_shared_sha256=hashlib.sha256(protected_text.encode()).hexdigest(),
                source_sha256=hashlib.sha256(original.encode()).hexdigest(), expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                authority_file=Path("manager_mail/85c5dff58359-1290.txt"), authority_lines=(3, 4),
                authority_sha256=hashlib.sha256(authority_text.encode()).hexdigest(), authority_envelope=Path("authority.md"),
                authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(), audit_output=private / "cancel.yaml",
            )
            original_reader = read_park_authority_envelope
            calls = 0

            def drift_authority(current_args: StatusArgs, excerpt: str, locator: str) -> str:
                nonlocal calls
                result = original_reader(current_args, excerpt, locator)
                calls += 1
                if calls == 1:
                    envelope.write_text(envelope_text + "drift\n", encoding="utf-8")
                return result

            with patch("omo_manager.omo_task_status.read_park_authority_envelope", side_effect=drift_authority):
                with self.assertRaises(TaskFrontmatterError):
                    cancel_shared_target_done(args, memory, original, memory.stat())
            self.assertEqual(original, memory.read_text(encoding="utf-8"))
            self.assertEqual(protected_text, protected.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_shared_target_cancellation_recovers_committed_prepared_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory_research_mgr.md"
            original = task_frontmatter(status="blocked", blocked_on="paused", pending_items=("one",), runat="wl:32", is_manager=True)
            memory.write_text(original, encoding="utf-8")
            protected = root / "transcription_sw.md"
            protected_text = task_frontmatter(status="blocked", blocked_on="human", pending_items=("keep",), runat="wl:32")
            protected.write_text(protected_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntranscription_sw.md wl:32\n\nhuman pending:\nmemory_research_mgr.md wl:32\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            mail = root / "manager_mail"
            mail.mkdir(mode=0o700)
            authority_text = "Subject: Re: Authorize memory_research_mgr.md relocation\n\nClose the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            authority = mail / "85c5dff58359-1290.txt"
            authority.write_text(authority_text, encoding="utf-8")
            authority.chmod(0o600)
            envelope = root / "authority.md"
            excerpt = "Close the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            envelope_text = '<human_instruction authoritative="true" source="manager_mail/85c5dff58359-1290.txt:3-4">\n' + excerpt + "</human_instruction>\n"
            envelope.write_text(envelope_text, encoding="utf-8")
            private = root / "private"
            private.mkdir(mode=0o700)
            args = StatusArgs(
                root, Path("memory_research_mgr.md"), "done", "", cancel_shared_target=True,
                shared_target="wl:32", protected_shared_task=Path("transcription_sw.md"), protected_shared_sha256=hashlib.sha256(protected_text.encode()).hexdigest(),
                source_sha256=hashlib.sha256(original.encode()).hexdigest(), expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                authority_file=Path("manager_mail/85c5dff58359-1290.txt"), authority_lines=(3, 4), authority_sha256=hashlib.sha256(authority_text.encode()).hexdigest(),
                authority_envelope=Path("authority.md"), authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(), audit_output=private / "cancel.yaml",
            )
            original_finish = finish_private_audit
            with patch("omo_manager.omo_task_status.finish_private_audit", side_effect=OSError("injected finalization failure")):
                self.assertEqual("wl:32", cancel_shared_target_done(args, memory, original, memory.stat()))
            prepared = (private / "cancel.yaml").read_text(encoding="utf-8")
            self.assertNotIn("final-result", prepared)
            with patch("omo_manager.omo_task_status.finish_private_audit", side_effect=original_finish):
                self.assertEqual("wl:32", cancel_shared_target_done(args, memory, original, memory.stat()))
            complete = (private / "cancel.yaml").read_text(encoding="utf-8")
            self.assertIn("final-result: success", complete)
            self.assertEqual("wl:32", cancel_shared_target_done(args, memory, original, memory.stat()))
            self.assertEqual(complete, (private / "cancel.yaml").read_text(encoding="utf-8"))
            todo.write_text(todo.read_text(encoding="utf-8") + "drift\n", encoding="utf-8")
            with self.assertRaises(TaskFrontmatterError):
                cancel_shared_target_done(args, memory, original, memory.stat())

    def test_shared_target_cancellation_recovers_todo_first_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = root / "memory_research_mgr.md"
            original = task_frontmatter(status="blocked", blocked_on="paused", pending_items=("one",), runat="wl:32", is_manager=True)
            memory.write_text(original, encoding="utf-8")
            protected = root / "transcription_sw.md"
            protected_text = task_frontmatter(status="blocked", blocked_on="human", pending_items=("keep",), runat="wl:32")
            protected.write_text(protected_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntranscription_sw.md wl:32\n\nhuman pending:\nmemory_research_mgr.md wl:32\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            mail = root / "manager_mail"
            mail.mkdir(mode=0o700)
            authority_text = "Subject: Re: Authorize memory_research_mgr.md relocation\n\nClose the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            authority = mail / "85c5dff58359-1290.txt"
            authority.write_text(authority_text, encoding="utf-8")
            authority.chmod(0o600)
            excerpt = "Close the “memory” thing. It is so old.\nWhich email report was for the transcription thing\n"
            envelope_text = '<human_instruction authoritative="true" source="manager_mail/85c5dff58359-1290.txt:3-4">\n' + excerpt + "</human_instruction>\n"
            (root / "authority.md").write_text(envelope_text, encoding="utf-8")
            private = root / "private"
            private.mkdir(mode=0o700)
            args = StatusArgs(
                root, Path("memory_research_mgr.md"), "done", "", cancel_shared_target=True,
                shared_target="wl:32", protected_shared_task=Path("transcription_sw.md"), protected_shared_sha256=hashlib.sha256(protected_text.encode()).hexdigest(),
                source_sha256=hashlib.sha256(original.encode()).hexdigest(), expected_todo_sha256=hashlib.sha256(todo_text.encode()).hexdigest(),
                authority_file=Path("manager_mail/85c5dff58359-1290.txt"), authority_lines=(3, 4), authority_sha256=hashlib.sha256(authority_text.encode()).hexdigest(),
                authority_envelope=Path("authority.md"), authority_envelope_sha256=hashlib.sha256(envelope_text.encode()).hexdigest(), audit_output=private / "cancel.yaml",
            )
            (private / "cancel.yaml").write_text("final-result: success\n", encoding="utf-8")
            (private / "cancel.yaml").chmod(0o600)
            with self.assertRaises(TaskFrontmatterError):
                cancel_shared_target_done(args, memory, original, memory.stat())
            (private / "cancel.yaml").unlink()
            real_replace = replace_if_unchanged_locked

            def fail_task_write(target: Path, replacement: str, state: os.stat_result) -> None:
                if target == memory:
                    raise OSError("simulated task write failure")
                real_replace(target, replacement, state)

            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_write):
                with self.assertRaises(OSError):
                    cancel_shared_target_done(args, memory, original, memory.stat())
            self.assertEqual(original, memory.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

            def crash_after_todo(target: Path, replacement: str, state: os.stat_result) -> None:
                if target == memory:
                    raise SystemExit("simulated process death")
                real_replace(target, replacement, state)

            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=crash_after_todo):
                with self.assertRaises(SystemExit):
                    cancel_shared_target_done(args, memory, original, memory.stat())
            self.assertEqual(original, memory.read_text(encoding="utf-8"))
            self.assertNotEqual(todo_text, todo.read_text(encoding="utf-8"))
            prepared = (private / "cancel.yaml").read_text(encoding="utf-8")
            forged = yaml.safe_load(prepared)
            forged_todo = str(forged["committed_todo_text"]).replace("transcription_sw.md wl:32", "transcription_sw.md wl:99")
            forged["committed_todo_text"] = forged_todo
            forged["committed_todo_sha256"] = hashlib.sha256(forged_todo.encode()).hexdigest()
            (private / "cancel.yaml").write_text(yaml.safe_dump(forged, sort_keys=False), encoding="utf-8")
            with self.assertRaises(TaskFrontmatterError):
                cancel_shared_target_done(args, memory, original, memory.stat())
            self.assertEqual(original, memory.read_text(encoding="utf-8"))
            (private / "cancel.yaml").write_text(prepared, encoding="utf-8")
            forged = yaml.safe_load(prepared)
            forged["cancelled_pending_items"] = ["different"]
            forged["cancelled_pending_items_sha256"] = hashlib.sha256(yaml.safe_dump(["different"], sort_keys=False).encode()).hexdigest()
            (private / "cancel.yaml").write_text(yaml.safe_dump(forged, sort_keys=False), encoding="utf-8")
            with self.assertRaises(TaskFrontmatterError):
                cancel_shared_target_done(args, memory, original, memory.stat())
            self.assertEqual(original, memory.read_text(encoding="utf-8"))
            (private / "cancel.yaml").write_text(prepared, encoding="utf-8")
            self.assertEqual("wl:32", cancel_shared_target_done(args, memory, original, memory.stat()))
            self.assertEqual("done", parse_task_metadata(memory.read_text(encoding="utf-8"), root).status)
            self.assertIn("final-result: success", (private / "cancel.yaml").read_text(encoding="utf-8"))

    def test_shared_target_cancellation_parser_requires_complete_mode(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--cancel-shared-target", "memory_research_mgr.md"])
        with self.assertRaises(SystemExit):
            parse_args(["--protected-shared-task", "transcription_sw.md", "memory_research_mgr.md"])

    def test_restore_terminal_target_changes_only_proven_runat_without_tmux_or_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "terminal.md"
            historical = task_frontmatter(status="done", runat="wl:7") + "body\n"
            path.write_text(historical)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "terminal.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            original = historical.replace("runat: wl:7", "runat: retired")
            path.write_text(original)
            todo = root / "TODO.md"
            todo.write_text("previous:\nother.md wl:3\n")
            args = StatusArgs(root, Path("terminal.md"), "", "", restore_terminal_target=True, historical_target="wl:7", task_sha256=hashlib.sha256(original.encode()).hexdigest(), historical_commit=commit)
            with patch("omo_manager.omo_task_status.stop") as stop:
                self.assertEqual(0, run(args))
            stop.assert_not_called()
            self.assertEqual(original.replace("runat: retired", "runat: wl:7"), path.read_text())
            self.assertEqual("previous:\nother.md wl:3\n", todo.read_text())

    def test_restore_terminal_target_refuses_digest_active_queue_and_nonterminal_status(self) -> None:
        cases = (
            (task_frontmatter(status="done", runat="retired"), "0" * 64, "digest"),
            (task_frontmatter(status="done", runat="retired", pending_items=("open",)), None, "empty queue"),
            (task_frontmatter(status="blocked", blocked_on="human", runat="retired"), None, "v1 done/retired"),
        )
        for original, digest, error in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "terminal.md"
                path.write_text(original)
                args = StatusArgs(root, path, "", "", restore_terminal_target=True, historical_target="wl:7", task_sha256=digest or hashlib.sha256(original.encode()).hexdigest(), historical_commit="a" * 40)
                with self.assertRaisesRegex(TaskFrontmatterError, error):
                    restore_terminal_target(args, path, original, path.stat())
                self.assertEqual(original, path.read_text())

    def test_restore_terminal_target_rejects_same_path_unrelated_historical_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "terminal.md"
            historical = task_frontmatter(status="done", runat="wl:7") + "unrelated old body\n"
            path.write_text(historical)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "terminal.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            current = task_frontmatter(status="done", runat="retired") + "different current body\n"
            path.write_text(current)
            args = StatusArgs(root, path, "", "", restore_terminal_target=True, historical_target="wl:7", historical_commit=commit, task_sha256=hashlib.sha256(current.encode()).hexdigest())
            with self.assertRaisesRegex(TaskFrontmatterError, "must equal current task bytes"):
                restore_terminal_target(args, path, current, path.stat())
            self.assertEqual(current, path.read_text())

    def test_restore_terminal_target_preserves_crlf_raw_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "terminal.md"
            historical = (task_frontmatter(status="done", runat="wl:7") + "body\n").replace("\n", "\r\n").encode()
            path.write_bytes(historical)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "false"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "terminal.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "historical"], check=True)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            current = historical.replace(b"runat: wl:7\r\n", b"runat: retired\r\n")
            path.write_bytes(current)
            args = StatusArgs(root, path, "", "", restore_terminal_target=True, historical_target="wl:7", historical_commit=commit, task_sha256=hashlib.sha256(current).hexdigest())
            self.assertEqual(0, run(args))
            self.assertEqual(historical, path.read_bytes())

    def test_parse_disables_target_retirement_and_requires_restore_evidence(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--root", "/tmp/work", "--retire-blocked-target", "--stale-target", "wl:2", "task.md"])
        with self.assertRaises(SystemExit):
            parse_args(["--root", "/tmp/work", "--restore-terminal-target", "--historical-target", "wl:2", "task.md"])
        parsed = parse_args(["--root", "/tmp/work", "--restore-terminal-target", "--historical-target", "wl:2", "--historical-commit", "b" * 40, "--task-sha256", "a" * 64, "task.md"])
        self.assertTrue(parsed.restore_terminal_target)
        with self.assertRaises(SystemExit):
            parse_args(["--root", "/tmp/work", "--restore-terminal-target", "--historical-target", "wl:2", "--historical-commit", "b" * 40, "--task-sha256", "a" * 64, "--blocked-on", "human", "task.md"])

    def test_repository_closure_custody_accepts_clean_or_exact_dirty_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "keep.txt").write_text("before\n")
            (repository / "remove.txt").write_text("remove\n")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], check=True)
            ensure_repository_closure_custody(repository, None)
            (repository / "keep.txt").write_text("after\n")
            (repository / "remove.txt").unlink()
            status = subprocess.run(["git", "-C", str(repository), "status", "--porcelain=v1", "-z", "--untracked-files=no"], check=True, capture_output=True).stdout
            receipt = Path(tmp) / "handoff.yaml"
            receipt.write_text(
                "version: v1.0.0\n"
                f"repository: {repository.resolve().as_posix()}\n"
                f"status_sha256: {hashlib.sha256(status).hexdigest()}\n"
                "assignments:\n"
                "  - path: keep.txt\n    state: ' M'\n    owner: cleanup-successor.md\n    evidence: durable manager handoff receipt 1\n"
                "  - path: remove.txt\n    state: ' D'\n    owner: cleanup-successor.md\n    evidence: durable manager handoff receipt 1\n"
            )
            ensure_repository_closure_custody(repository, receipt)

    def test_repository_closure_custody_fails_closed_on_unassigned_or_drifted_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "tracked.txt").write_text("before\n")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], check=True)
            (repository / "tracked.txt").write_text("after\n")
            with self.assertRaisesRegex(TaskFrontmatterError, "explicit dirty-path ownership handoff"):
                ensure_repository_closure_custody(repository, None)
            receipt = Path(tmp) / "handoff.yaml"
            receipt.write_text(
                "version: v1.0.0\n"
                f"repository: {repository.resolve().as_posix()}\n"
                f"status_sha256: {'0' * 64}\n"
                "assignments:\n"
                "  - path: tracked.txt\n    state: ' M'\n    owner: successor.md\n    evidence: reviewed handoff\n"
            )
            with self.assertRaisesRegex(TaskFrontmatterError, "does not bind"):
                ensure_repository_closure_custody(repository, receipt)

    def test_repository_closure_custody_requires_root_and_tracks_rename_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "old.txt").write_text("content\n")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], check=True)
            (repository / "subdir").mkdir()
            with self.assertRaisesRegex(TaskFrontmatterError, "exact Git worktree root"):
                ensure_repository_closure_custody(repository / "subdir", None)
            subprocess.run(["git", "-C", str(repository), "mv", "old.txt", "new.txt"], check=True)
            _, states = tracked_dirty_state(repository)
            self.assertEqual({"new.txt": "R "}, states)

    def test_repository_closure_custody_rejects_non_utf8_path_and_relative_cli_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            raw_path = os.fsencode(repository) + b"/bad-\xff.txt"
            descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(descriptor)
            subprocess.run(["git", "-C", str(repository), "add", "-A"], check=True)
            with self.assertRaisesRegex(TaskFrontmatterError, "must be UTF-8"):
                tracked_dirty_state(repository)
        with self.assertRaises(SystemExit):
            parse_args(["--root", "/tmp/work", "--closure-repository", "relative/repo", "task.md", "done"])

    def test_repository_closure_refusal_precedes_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "task.md"
            task_text = task_frontmatter(status="running")
            task_path.write_text(task_text)
            (root / "TODO.md").write_text("current:\ntask.md wl:2\n\nprevious:\n")
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "tracked.txt").write_text("before\n")
            subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], check=True)
            (repository / "tracked.txt").write_text("after\n")
            args = StatusArgs(root, Path("task.md"), "done", "", closure_repository=repository)
            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_agent:
                self.assertEqual(2, run(args))
            stop_agent.assert_not_called()
            self.assertEqual(task_text, task_path.read_text())
            self.assertEqual("current:\ntask.md wl:2\n\nprevious:\n", (root / "TODO.md").read_text())

    def test_reconcile_long_running_human_index_preserves_task_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            text = "---\nversion: v1.0.0\nstatus: long_running\nblocked_on: human\nrunat: wl:2\ntool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items:\n  - wait\n---\nbody\n"
            path.write_text(text)
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md wl:2\nother.md wl:3\n\nhuman pending:\nwaiting.md wl:4\n")
            reconcile_long_running_human_index(root, path, text, path.stat())
            self.assertEqual(text, path.read_text())
            self.assertEqual("current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\nwaiting.md wl:4\n", todo.read_text())

    def test_reconcile_long_running_human_index_rejects_nonexact_human_and_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            text = "---\nversion: v1.0.0\nstatus: long_running\nblocked_on: human review\nrunat: wl:2\ntool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n---\n"
            path.write_text(text)
            (root / "TODO.md").write_text("current:\ntask.md wl:2\ntask.md wl:2\n\nhuman pending:\n")
            with self.assertRaisesRegex(TaskFrontmatterError, "blocked exactly on human"):
                reconcile_long_running_human_index(root, path, text, path.stat())
            exact = text.replace("human review", "human")
            path.write_text(exact)
            with self.assertRaisesRegex(TaskFrontmatterError, "exactly one TODO row"):
                reconcile_long_running_human_index(root, path, exact, path.stat())
            (root / "TODO.md").write_text("current:\n\nhuman pending:\ntask.md wl:2\n")
            with self.assertRaisesRegex(TaskFrontmatterError, "expected.*current"):
                reconcile_long_running_human_index(root, path, exact, path.stat())

    def test_reconcile_long_running_human_index_rejects_v2_without_actor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            text = """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000001
status: long_running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
blocked_on:
  - kind: persistent
    reason: human
pending_task_items: []
resolved_task_items: []
---
"""
            path.write_text(text)
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n")
            (root / "TODO.md").write_text("current:\ntask.md wl:2\n\nhuman pending:\n")
            args = StatusArgs(root, path, "", "", reconcile_long_running_human_index=True)
            with patch("omo_manager.omo_task_status.blocking_request") as actor:
                self.assertEqual(2, run(args))
            actor.assert_not_called()
            self.assertEqual(text, path.read_text())
            self.assertEqual("current:\ntask.md wl:2\n\nhuman pending:\n", (root / "TODO.md").read_text())
    def test_stop_done_agent_treats_verified_missing_exact_pane_as_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), patch("omo_manager.omo_task_status.stop") as stop_call:
                stop_args, session_id = stop_done_agent(root, path, metadata)

            stop_call.assert_not_called()
            self.assertEqual("wl:3", stop_args.target)
            self.assertEqual("", session_id)

    def test_cli_done_closes_verified_missing_pane_and_moves_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "manager.md"
            path.write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nmanager.md wl:3\n\nprevious:\n", encoding="utf-8")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop") as stop_call,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(0, exit_code)
            stop_call.assert_not_called()
            self.assertIn("status: done\nrunat: wl:3", path.read_text(encoding="utf-8"))
            self.assertIn("tmux target `wl:3`; Codex session id not found", path.read_text(encoding="utf-8"))
            self.assertIn("current:\n\nprevious:\nmanager.md wl:3\n", todo.read_text(encoding="utf-8"))

    def test_stop_done_agent_accepts_disappearance_during_status_paste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None
            paste_error = subprocess.CalledProcessError(1, ["tmux", "paste-buffer", "-t", "%3"])

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=("%3", "")),
                patch("omo_manager.omo_task_status.pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop", side_effect=paste_error) as stop_call,
            ):
                stop_args, session_id = stop_done_agent(root, path, metadata)

            self.assertEqual("%3", stop_call.call_args.args[0].target)
            self.assertEqual("wl:3", stop_args.target)
            self.assertEqual("", session_id)

    def test_stop_done_agent_propagates_failure_for_live_or_reused_pane(self) -> None:
        for current_pane_id, original_pane_id in (("%3", "%3"), ("%4", ""), ("", "%3")):
            with self.subTest(current_pane_id=current_pane_id, original_pane_id=original_pane_id), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=("%3", current_pane_id)),
                    patch("omo_manager.omo_task_status.pane_id", return_value=original_pane_id),
                    patch("omo_manager.omo_task_status.stop", side_effect=RuntimeError("status paste failed")),
                    self.assertRaisesRegex(RuntimeError, "status paste failed"),
                ):
                    stop_done_agent(root, path, metadata)

    def test_stop_done_agent_allows_unique_current_worker_to_close_own_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            (root / "stale_done.md").write_text(task_frontmatter(status="done", runat="wl:3") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:3\nstale_done.md wl:3\n\nhuman pending:\nstale.md\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch("omo_manager.omo_task_status.stop", return_value="session-1") as stop_call:
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertTrue(stop_args.allow_self)
            self.assertEqual("wl:3", stop_args.target)
            self.assertEqual("%3", stop_call.call_args.args[0].target)
            self.assertTrue(stop_call.call_args.args[0].allow_self)

    def test_stop_done_agent_ignores_valid_noncurrent_target_reuse(self) -> None:
        for section in ("Previous", "Human Pending"):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
                (root / "stale.md").write_text(task_frontmatter(status="blocked", blocked_on="waiting for human", runat="wl:3") + "body\n", encoding="utf-8")
                (root / "TODO.md").write_text(f"Current\nworker.md wl 3\n\n{section}:\nstale.md wl:3\n", encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                    stop_args, _session_id = stop_done_agent(root, path, metadata)

                self.assertTrue(stop_args.allow_self)

    def test_stop_done_agent_refuses_malformed_current_record(self) -> None:
        for todo_line in ("broken.md wl:3", "broken.md wl 3"):
            with self.subTest(todo_line=todo_line), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
                (root / "broken.md").write_text("no frontmatter\n", encoding="utf-8")
                (root / "TODO.md").write_text(f"current:\nworker.md wl:3\n{todo_line}\n", encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                    stop_args, _session_id = stop_done_agent(root, path, metadata)

                self.assertFalse(stop_args.allow_self)

    def test_stop_done_agent_rechecks_current_ownership_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            other = root / "other.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value="%3"), patch(
                "omo_manager.omo_task_status.current_target_task_paths", side_effect=((path,), (other, path))
            ), patch(
                "omo_manager.omo_task_status.stop", return_value="session-1"
            ):
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)

    def test_stop_done_agent_refuses_self_close_after_pane_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:3\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=("%3", "%4")), patch("omo_manager.omo_task_status.stop", return_value="session-1") as stop_call:
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)
            self.assertEqual("%3", stop_call.call_args.args[0].target)

    def test_stop_done_agent_does_not_fall_back_to_prefix_resolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:1", managerat="main:0") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:1\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), patch("omo_manager.omo_task_status.stop", return_value="session-1") as stop_call:
                stop_args, session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)
            self.assertEqual("", session_id)
            stop_call.assert_not_called()

    def test_stop_done_agent_refuses_self_close_for_manager_or_current_collision(self) -> None:
        cases = {
            "manager task": (True, "current:\nworker.md wl:3\n"),
            "current collision": (False, "current:\nworker.md wl:3\nmanager.md wl:3\n"),
        }
        for name, (is_manager, todo_text) in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "worker.md"
                path.write_text(task_frontmatter(runat="wl:3", is_manager=is_manager) + "body\n", encoding="utf-8")
                (root / "manager.md").write_text(task_frontmatter(runat="wl:3", is_manager=True) + "body\n", encoding="utf-8")
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
                assert metadata is not None

                with patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                    stop_args, _session_id = stop_done_agent(root, path, metadata)

                self.assertFalse(stop_args.allow_self)

    def test_stop_done_agent_refuses_configured_main_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "worker.md"
            path.write_text(task_frontmatter(runat="wl:3") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nworker.md wl:3\n", encoding="utf-8")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            assert metadata is not None

            with patch.dict(os.environ, {"OMO_MANAGER_TMUX_TARGET": "wl:3"}), patch("omo_manager.omo_task_status.stop", return_value="session-1"):
                stop_args, _session_id = stop_done_agent(root, path, metadata)

            self.assertFalse(stop_args.allow_self)

    def test_sets_blocked_status_and_blocked_on(self) -> None:
        text = task_frontmatter() + "body\n"

        updated = update_frontmatter_status(text, "blocked", "waiting on human")

        self.assertIn("status: blocked\nblocked_on: waiting on human\n", updated)
        self.assertIn("body\n", updated)

    def test_running_removes_blocked_on(self) -> None:
        text = task_frontmatter(status="blocked", blocked_on="waiting on human") + "body\n"

        updated = update_frontmatter_status(text, "running", "")

        self.assertIn("status: running\nrunat:", updated)
        self.assertNotIn("blocked_on:", updated)

    def test_cli_running_moves_the_single_previous_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(runat="vl_root_consolidate:1", managerat="vl_stage1_mgr:0", pending_items=("keep the queue",)) + "all task content stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text(
                "preamble stays\ncurrent:\n\ncurrent.md wl:9 unchanged\n\nhuman pending:\nwaiting.md wl:4 unchanged\n\nprevious:\nold.md wl:2 unchanged\n  task.md vl_root_consolidate:1 keep this row exactly\nlater.md wl:3 unchanged\n",
                encoding="utf-8",
            )
            expected = "preamble stays\ncurrent:\n\n  task.md vl_root_consolidate:1 keep this row exactly\ncurrent.md wl:9 unchanged\n\nhuman pending:\nwaiting.md wl:4 unchanged\n\nprevious:\nold.md wl:2 unchanged\nlater.md wl:3 unchanged\n"

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))
            self.assertEqual(1, todo.read_text(encoding="utf-8").count("task.md vl_root_consolidate:1 keep this row exactly"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()

            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))

    def test_cli_running_transition_moves_single_human_pending_row_and_preserves_task_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human", runat="pb:14", managerat="pbsocialcli:0", pending_items=("keep the queue",)) + "all task content stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nother.md wl:9\n\nhuman pending:\ntask.md\n\nprevious:\nold.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            updated_task = path.read_text(encoding="utf-8")
            self.assertIn("status: running\nrunat: pb:14", updated_task)
            self.assertNotIn("blocked_on:", updated_task)
            self.assertIn("pending_task_items:\n  - keep the queue", updated_task)
            self.assertTrue(updated_task.endswith("all task content stays\n"))
            self.assertEqual("current:\ntask.md\nother.md wl:9\n\nhuman pending:\n\nprevious:\nold.md wl:2\n", todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()

    def test_cli_running_transition_refuses_duplicate_or_mismatched_human_pending_row(self) -> None:
        cases = {
            "duplicate": "current:\ntask.md wl:2\n\nhuman pending:\ntask.md wl:2\n",
            "mismatched target": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:3\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                todo.write_text(todo_text, encoding="utf-8")

                with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "running", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_running_transition_rolls_back_todo_when_task_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")

            def fail_task_replace(target: Path, text: str, before: os.stat_result) -> None:
                if target == path:
                    raise OSError("task replace failed")
                replace_if_unchanged_locked(target, text, before)

            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_replace), redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_cli_blocked_moves_live_rednote_path_only_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "rednote-recovery.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human", runat="social:4", managerat="social-manager:1", pending_items=("human RedNote sign-in and read-only verification",)) + "all task content stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nrednote-recovery.md\nother.md wl:9\n\nhuman pending:\nwaiting.md wl:4\n\nprevious:\nold.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("rednote-recovery.md"), "blocked", "human")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual("current:\nother.md wl:9\n\nhuman pending:\nrednote-recovery.md\nwaiting.md wl:4\n\nprevious:\nold.md wl:2\n", todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()

    def test_cli_blocked_fails_closed_for_invalid_todo_placement(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nhuman pending:\nwaiting.md wl:3\n",
            "duplicate": "current:\ntask.md wl:2\n\nhuman pending:\ntask.md wl:2\n",
            "ambiguous row": "current:\ntask.md wl:2 task.md wl:2\n\nhuman pending:\n",
            "mismatched target": "current:\ntask.md wl:3\n\nhuman pending:\n",
            "target before task": "current:\nwl:2 task.md\n\nhuman pending:\n",
            "description before task": "current:\nnotes task.md\n\nhuman pending:\n",
            "malformed task suffix": "current:\ntask.mdx\n\nhuman pending:\n",
            "blocked annotation": "current:\ntask.md wl:2 (blocked: human)\n\nhuman pending:\n",
            "done annotation": "current:\ntask.md wl:2 (done)\n\nhuman pending:\n",
            "duplicate under unknown heading": "current:\ntask.md wl:2\n\nhuman pending:\n\nblocked:\ntask.md wl:2\n",
            "previous": "current:\nother.md wl:2\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n",
            "low priority": "current:\nother.md wl:2\n\nhuman pending:\n\nlow priority:\ntask.md wl:2\n",
            "duplicate destination": "current:\ntask.md wl:2\n\nhuman pending:\n\nhuman pending:\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                if todo_text is not None:
                    todo.write_text(todo_text, encoding="utf-8")

                with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "blocked", "human")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text is not None, todo.exists())
                if todo_text is not None:
                    self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_blocked_reconciliation_updates_changed_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nhuman pending:\n"
            todo.write_text(todo_text, encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "blocked", "different human reason")))

            self.assertEqual(original_task.replace("blocked_on: human", "blocked_on: different human reason"), path.read_text(encoding="utf-8"))
            self.assertEqual("current:\n\nhuman pending:\ntask.md wl:2\n", todo.read_text(encoding="utf-8"))

    def test_cli_blocked_reconciliation_rolls_back_todo_when_blocker_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nhuman pending:\n"
            todo.write_text(todo_text, encoding="utf-8")

            def fail_task_replace(target: Path, text: str, before: os.stat_result) -> None:
                if target == path:
                    raise OSError("task replace failed")
                replace_if_unchanged_locked(target, text, before)

            with patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_replace), redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "blocked", "different human reason")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_cli_done_reissue_moves_live_rednote_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "pb_social_followup.md"
            original_task = (
                task_frontmatter(status="done", runat="pb:2", managerat="wl:6")
                + "(Completed RedNote recovery: bounded live public search succeeded.)\n\n"
                + "(manager closed Codex agent 08-06 11:47 PDT; tmux target `pb:2`; session_id: `session-1`.)\n"
            )
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\npb_spectrum_news.md\n\nhuman pending:\npb_social_followup.md pb:2\nwaiting.md wl:4\n\nprevious:\nold.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")
            expected = "current:\npb_spectrum_news.md\n\nhuman pending:\nwaiting.md wl:4\n\nprevious:\npb_social_followup.md pb:2\nold.md wl:2\n"

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                patch("omo_manager.omo_task_status.stop") as stop_agent,
                patch("omo_manager.omo_task_status.record_close") as record_close_call,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(StatusArgs(root, Path("pb_social_followup.md"), "done", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(expected, todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()
            record_close_call.assert_not_called()

    def test_cli_done_reissue_moves_low_priority_row_without_task_or_pane_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vl_paper_intake14.md"
            original_task = task_frontmatter(status="done", runat="vl:2", managerat="wl:30") + "close history stays\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\n\nlow priority:\nvl_paper_intake14.md vl:2\n\nhuman pending:\n\nprevious:\n", encoding="utf-8")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                patch("omo_manager.omo_task_status.stop") as stop_agent,
                patch("omo_manager.omo_task_status.record_close") as record_close_call,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(StatusArgs(root, Path("vl_paper_intake14.md"), "done", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(
                "current:\n\nlow priority:\n\nhuman pending:\n\nprevious:\nvl_paper_intake14.md vl:2\n",
                todo.read_text(encoding="utf-8"),
            )
            stop_done_agent.assert_not_called()
            stop_agent.assert_not_called()
            record_close_call.assert_not_called()

    def test_cli_done_reissue_fails_closed_for_invalid_todo_or_task_state(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nhuman pending:\n\nprevious:\n",
            "duplicate": "current:\ntask.md wl:2\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n",
            "ambiguous row": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2 task.md wl:2\n\nprevious:\n",
            "mismatched target": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:3\n\nprevious:\n",
            "target before task": "current:\nother.md wl:3\n\nhuman pending:\nwl:2 task.md\n\nprevious:\n",
            "description before task": "current:\nother.md wl:3\n\nhuman pending:\nnotes task.md wl:2\n\nprevious:\n",
            "malformed task suffix": "current:\nother.md wl:3\n\nhuman pending:\ntask.mdx wl:2\n\nprevious:\n",
            "blocked annotation": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2 (blocked: stale)\n\nprevious:\n",
            "done annotation": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2 (done)\n\nprevious:\n",
            "already previous": "current:\nother.md wl:3\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n",
            "duplicate destination": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n\nprevious:\n",
            "mixed-case duplicate destination": "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n\nPrevious:\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="done") + "close history stays\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                if todo_text is not None:
                    todo.write_text(todo_text, encoding="utf-8")

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                    patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                    patch("omo_manager.omo_task_status.stop") as stop_agent,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text is not None, todo.exists())
                if todo_text is not None:
                    self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()
                stop_agent.assert_not_called()

        for name, original_task in {
            "nonempty queue": task_frontmatter(status="done", pending_items=("still open",)) + "history\n",
            "live pending marker": task_frontmatter(status="done") + "(pending)\nnew request\n",
            "noncanonical bytes": (task_frontmatter(status="done") + "history\n").replace("status: done\n", "status: done  \n"),
        }.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                todo_text = "current:\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
                todo.write_text(todo_text, encoding="utf-8")

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                    patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_done_reissue_rejects_active_pane_or_current_owner(self) -> None:
        for name, pane, owner_runat in (("active pane", "%2", ""), ("active current owner", "", "wl:2"), ("conflicting current TODO owner", "", "wl:3")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter(status="done") + "history\n"
                path.write_text(original_task, encoding="utf-8")
                owner_row = "owner.md wl:2\n" if owner_runat else ""
                if owner_runat:
                    (root / "owner.md").write_text(task_frontmatter(runat=owner_runat) + "active\n", encoding="utf-8")
                todo = root / "TODO.md"
                todo_text = f"current:\n{owner_row}\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
                todo.write_text(todo_text, encoding="utf-8")

                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", return_value=pane),
                    patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_done_index_reconciliation_rechecks_task_and_todo_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="done") + "history\n"
            path.write_text(original_task, encoding="utf-8")
            before = path.stat()
            todo = root / "TODO.md"
            todo_text = "current:\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
            todo.write_text(todo_text, encoding="utf-8")
            changed_task = original_task + "concurrent note\n"
            path.write_text(changed_task, encoding="utf-8")

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), self.assertRaisesRegex(TaskFrontmatterError, "task changed"):
                reconcile_done_index(root, path, original_task, before)

            self.assertEqual(changed_task, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="done") + "history\n"
            path.write_text(original_task, encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\n\nhuman pending:\ntask.md wl:2\n\nprevious:\n"
            concurrent_todo = todo_text + "concurrent row\n"
            todo.write_text(todo_text, encoding="utf-8")

            def race_todo(*args: object) -> str:
                todo.write_text(concurrent_todo, encoding="utf-8")
                return "current:\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n"

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=""),
                patch("omo_manager.omo_task_status.reconcile_done_todo_text", side_effect=race_todo),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "done", "")))

            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(concurrent_todo, todo.read_text(encoding="utf-8"))

    def test_cli_running_to_done_keeps_normal_close_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md wl:2\n\nprevious:\n", encoding="utf-8")
            close_args = StopArgs("wl:2", 10.0, 2000, False, False, root, "task.md", True, 0.0)

            with (
                patch("omo_manager.omo_task_status.stop_done_agent", return_value=(close_args, "session-1")) as stop_done_agent,
                patch("omo_manager.omo_task_status.record_close"),
                patch("omo_manager.omo_task_status.reconcile_done_index") as reconcile_done,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "done", "")))

            stop_done_agent.assert_called_once()
            reconcile_done.assert_not_called()
            self.assertIn("status: done", path.read_text(encoding="utf-8"))

    def test_cli_running_fails_closed_for_invalid_todo_placement(self) -> None:
        cases = {
            "absent TODO": None,
            "missing": "current:\nother.md wl:2\n\nprevious:\nold.md wl:1\n",
            "duplicate": "current:\ntask.md wl:2\n\nprevious:\ntask.md wl:2\n",
            "ambiguous row": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2 task.md wl:2\n",
            "mismatched target": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:3\n",
            "target before task": "current:\nother.md wl:2\n\nhuman pending:\nwl:3 task.md\n",
            "description before task": "current:\nother.md wl:2\n\nhuman pending:\nnotes task.md\n",
            "malformed task suffix": "current:\nother.md wl:2\n\nhuman pending:\ntask.mdx\n",
            "blocked annotation": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2 (blocked: human)\n",
            "done annotation": "current:\nother.md wl:2\n\nhuman pending:\ntask.md wl:2 (done)\n",
            "uppercase done annotation": "current:\nother.md wl:2\n\nhuman pending:\ntask.md (DONE)\n",
            "blocked section": "current:\nother.md wl:2\n\nblocked:\ntask.md wl:2\n",
            "done section": "current:\nother.md wl:2\n\ndone:\ntask.md wl:2\n",
            "unknown nested section": "current:\nother.md wl:2\n\nhuman pending:\nnotes:\ntask.md wl:2\n",
            "low priority": "current:\nother.md wl:2\n\nlow priority:\ntask.md wl:2\n",
            "duplicate current": "current:\ntask.md wl:2\n\ncurrent:\nother.md wl:3\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original_task = task_frontmatter() + "body\n"
                path.write_text(original_task, encoding="utf-8")
                todo = root / "TODO.md"
                if todo_text is not None:
                    todo.write_text(todo_text, encoding="utf-8")
                stderr = io.StringIO()

                with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stderr(stderr):
                    self.assertEqual(2, run(StatusArgs(root, Path("task.md"), "running", "")))

                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text is not None, todo.exists())
                if todo_text is not None:
                    self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
                stop_done_agent.assert_not_called()

    def test_cli_running_transition_moves_previous_row_to_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")
            todo = root / "TODO.md"
            todo_text = "current:\nother.md wl:2\n\nprevious:\ntask.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent") as stop_done_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "running", "")))

            self.assertIn("status: running", path.read_text(encoding="utf-8"))
            self.assertEqual("current:\ntask.md wl:2\nother.md wl:2\n\nprevious:\n", todo.read_text(encoding="utf-8"))
            stop_done_agent.assert_not_called()

    def test_running_index_reconciliation_rechecks_the_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter() + "body\n"
            path.write_text(original_task, encoding="utf-8")
            before = path.stat()
            todo = root / "TODO.md"
            todo_text = "current:\n\nprevious:\ntask.md wl:2\n"
            todo.write_text(todo_text, encoding="utf-8")
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "task changed while running index reconciliation"):
                reconcile_running_index(root, path, original_task, before)

            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_running_index_reconciliation_deduplicates_a_todo_task_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            todo.write_text(task_frontmatter() + "current:\n\nprevious:\nTODO.md wl:2\n", encoding="utf-8")
            text = todo.read_text(encoding="utf-8")

            reconcile_running_index(root, todo, text, todo.stat())

            self.assertIn("current:\n\nTODO.md wl:2\n", todo.read_text(encoding="utf-8"))

    def test_blocked_index_reconciliation_rechecks_the_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
            path.write_text(original_task, encoding="utf-8")
            before = path.stat()
            todo = root / "TODO.md"
            todo_text = "current:\ntask.md wl:2\n\nhuman pending:\n"
            todo.write_text(todo_text, encoding="utf-8")
            path.write_text(task_frontmatter(status="blocked", blocked_on="different") + "body\n", encoding="utf-8")

            with self.assertRaisesRegex(TaskFrontmatterError, "task changed or no longer matches"):
                reconcile_blocked_index(root, path, original_task, original_task, before)

            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))

    def test_cli_blocked_reconciliation_moves_previous_row_to_human_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            task_text = task_frontmatter(status="blocked", blocked_on="dependency", pending_items=("evaluate",)) + "body\n"
            path.write_text(task_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nother.md wl:3\n\nhuman pending:\nwaiting.md wl:4\n\nprevious:\ntask.md wl:2\n", encoding="utf-8")

            args = StatusArgs(root, Path("task.md"), "", "", reconcile_blocked_index=True, source_sha256=hashlib.sha256(task_text.encode()).hexdigest())
            with patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(args))

            self.assertEqual(task_text, path.read_text(encoding="utf-8"))
            self.assertEqual(
                "current:\nother.md wl:3\n\nhuman pending:\ntask.md wl:2\nwaiting.md wl:4\n\nprevious:\n",
                todo.read_text(encoding="utf-8"),
            )
            stop_agent.assert_not_called()

    def test_cli_blocked_reconciliation_moves_low_priority_row_without_changing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vl_target_select27.md"
            blocker = "final STOP report has only replay commitment; accepted delivery and exact consumed-closure attestation are absent"
            task_text = task_frontmatter(
                status="blocked",
                blocked_on=blocker,
                runat="vl_build_mgr:4",
                managerat="vl_build_mgr:3",
                pending_items=("completed item 1", "completed item 2", "completed item 3", "completed item 4", "completed item 5", "completed item 6"),
            ) + "body\n"
            path.write_text(task_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nother.md wl:3\n\nhuman pending:\nwaiting.md wl:4\n\nlow priority:\nvl_target_select27.md vl_build_mgr:4\n\nprevious:\n", encoding="utf-8")

            args = StatusArgs(root, Path("vl_target_select27.md"), "", "", reconcile_blocked_index=True, source_sha256=hashlib.sha256(task_text.encode()).hexdigest())
            with patch("omo_manager.omo_task_status.stop") as stop_agent, redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(args))

            self.assertEqual(task_text, path.read_text(encoding="utf-8"))
            self.assertEqual(
                "current:\nother.md wl:3\n\nhuman pending:\nvl_target_select27.md vl_build_mgr:4\nwaiting.md wl:4\n\nlow priority:\n\nprevious:\n",
                todo.read_text(encoding="utf-8"),
            )
            stop_agent.assert_not_called()

    def test_blocked_previous_reconciliation_rejects_ineligible_records(self) -> None:
        cases = {
            "empty queue": task_frontmatter(status="blocked", blocked_on="dependency"),
            "manager": task_frontmatter(status="blocked", blocked_on="dependency", is_manager=True, pending_items=("evaluate",)),
            "failed closure": task_frontmatter(status="blocked", blocked_on="done_close_failed: stop failed", pending_items=("evaluate",)),
            "human target": task_frontmatter(status="blocked", blocked_on="dependency", runat="hwork:1", pending_items=("evaluate",)),
            "retired target": task_frontmatter(status="blocked", blocked_on="dependency", runat="retired", pending_items=("evaluate",)),
            "v2": v2_task().replace("status: running", "status: blocked").replace("pending_task_items: []", "pending_task_items:\n  - evaluate"),
        }
        for name, task_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                path.write_text(task_text, encoding="utf-8")
                todo_text = "current:\n\nhuman pending:\n\nprevious:\ntask.md wl:2\n"
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                if name == "v2":
                    (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
                args = StatusArgs(root, Path("task.md"), "", "", reconcile_blocked_index=True, source_sha256=hashlib.sha256(task_text.encode()).hexdigest())

                with patch("omo_manager.omo_task_status.stop") as stop_agent, patch("omo_manager.omo_task_status.blocking_request") as blocking_request, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))

                self.assertEqual(task_text, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_text, (root / "TODO.md").read_text(encoding="utf-8"))
                stop_agent.assert_not_called()
                blocking_request.assert_not_called()

    def test_blocked_previous_reconciliation_rejects_ambiguous_todo(self) -> None:
        cases = {
            "duplicate row": "current:\ntask.md wl:2\n\nhuman pending:\n\nlow priority:\n\nprevious:\ntask.md wl:2\n",
            "duplicate previous": "current:\n\nhuman pending:\n\nlow priority:\n\nprevious:\ntask.md wl:2\n\nprevious:\n",
            "duplicate low priority": "current:\n\nhuman pending:\n\nlow priority:\ntask.md wl:2\n\nlow priority:\n\nprevious:\n",
            "case variant low priority": "current:\n\nhuman pending:\n\nLOW PRIORITY:\ntask.md wl:2\n\nprevious:\n",
            "wrong target": "current:\n\nhuman pending:\n\nlow priority:\n\nprevious:\ntask.md wl:3\n",
        }
        for name, todo_text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                task_text = task_frontmatter(status="blocked", blocked_on="dependency", pending_items=("evaluate",))
                path.write_text(task_text, encoding="utf-8")
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                args = StatusArgs(root, Path("task.md"), "", "", reconcile_blocked_index=True, source_sha256=hashlib.sha256(task_text.encode()).hexdigest())

                with redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))

                self.assertEqual(todo_text, (root / "TODO.md").read_text(encoding="utf-8"))

    def test_blocked_previous_reconciliation_rejects_todo_as_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            text = task_frontmatter(status="blocked", blocked_on="dependency", pending_items=("evaluate",)) + "current:\n\nhuman pending:\n\nprevious:\nTODO.md wl:2\n"
            todo.write_text(text, encoding="utf-8")
            args = StatusArgs(root, Path("TODO.md"), "", "", reconcile_blocked_index=True, source_sha256=hashlib.sha256(text.encode()).hexdigest())

            with redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))

            self.assertEqual(text, todo.read_text(encoding="utf-8"))

    def test_cli_blocked_reconciliation_combines_blocker_and_row_updates_when_task_is_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            todo = root / "TODO.md"
            original = task_frontmatter(status="blocked", blocked_on="human") + "current:\nTODO.md wl:2\n\nhuman pending:\n"
            todo.write_text(original, encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                self.assertEqual(0, run(StatusArgs(root, Path("TODO.md"), "blocked", "different human reason")))

            self.assertEqual(
                original.replace("blocked_on: human", "blocked_on: different human reason").replace(
                    "current:\nTODO.md wl:2\n\nhuman pending:\n",
                    "current:\n\nhuman pending:\nTODO.md wl:2\n",
                ),
                todo.read_text(encoding="utf-8"),
            )

    def test_cli_retires_blocked_human_target_without_tmux_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            original_task = task_frontmatter(
                status="blocked",
                blocked_on="human",
                pending_items=("first question", "second question"),
            ) + "closure evidence\nbody\n"
            path.write_text(original_task, encoding="utf-8")
            owner = root / "owner.md"
            owner_text = task_frontmatter(status="long_running", blocked_on="persistent manager role", is_manager=True) + "owner body\n"
            owner.write_text(owner_text, encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\nowner.md wl:2\n\nhuman pending:\n`stale.md` wl:2\n", encoding="utf-8")

            with (
                patch("omo_manager.omo_task_status.exact_pane_id") as exact_pane_id,
                patch("omo_manager.omo_task_status.stop") as stop,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target="wl:2", retire_blocked_target=True))

            self.assertEqual(2, exit_code)
            exact_pane_id.assert_not_called()
            stop.assert_not_called()
            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(owner_text, owner.read_text(encoding="utf-8"))
            self.assertEqual("current:\nowner.md wl:2\n\nhuman pending:\n`stale.md` wl:2\n", todo.read_text(encoding="utf-8"))

    def test_cli_target_retirement_fails_closed_for_invalid_preconditions(self) -> None:
        cases = {
            "manager": task_frontmatter(status="blocked", blocked_on="human", is_manager=True),
            "wrong blocker": task_frontmatter(status="blocked", blocked_on="dependency"),
            "human target": task_frontmatter(status="blocked", blocked_on="human", runat="hcfg:2"),
            "empty queue": task_frontmatter(status="blocked", blocked_on="human"),
        }
        for name, stale_frontmatter in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "stale.md"
                stale_target = "hcfg:2" if name == "human target" else "wl:2"
                original_task = stale_frontmatter + "body\n"
                path.write_text(original_task, encoding="utf-8")
                (root / "owner.md").write_text(task_frontmatter(runat=stale_target) + "owner\n", encoding="utf-8")
                todo = root / "TODO.md"
                original_todo = f"current:\nowner.md {stale_target}\n\nhuman pending:\nstale.md {stale_target}\n"
                todo.write_text(original_todo, encoding="utf-8")

                with redirect_stderr(io.StringIO()):
                    exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target=stale_target, retire_blocked_target=True))

                self.assertEqual(2, exit_code)
                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_cli_target_retirement_rejects_v2_before_mutation_or_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "stale.md"
            original_task = v2_task().replace(
                "  - kind: pending_items\n    item_ids: [pi_019f0000-0000-7000-8000-000000000003]\n",
                "",
            )
            path.write_text(original_task, encoding="utf-8")
            (root / "owner.md").write_text(task_frontmatter() + "owner\n", encoding="utf-8")
            todo = root / "TODO.md"
            original_todo = "current:\nowner.md wl:2\n\nhuman pending:\nstale.md wl:2\n"
            todo.write_text(original_todo, encoding="utf-8")
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.blocking_request") as reconcile, redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target="wl:2", retire_blocked_target=True))

            self.assertEqual(2, exit_code)
            reconcile.assert_not_called()
            self.assertEqual(original_task, path.read_text(encoding="utf-8"))
            self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_cli_target_retirement_rejects_missing_duplicate_or_nonlive_owner(self) -> None:
        for name, owner_status, duplicate in (
            ("missing", None, False),
            ("duplicate", "running", True),
            ("blocked owner", "blocked", False),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "stale.md"
                original_task = task_frontmatter(status="blocked", blocked_on="human") + "body\n"
                path.write_text(original_task, encoding="utf-8")
                owner_rows = ""
                if owner_status is not None:
                    blocker = "dependency" if owner_status == "blocked" else ""
                    (root / "owner.md").write_text(task_frontmatter(status=owner_status, blocked_on=blocker) + "owner\n", encoding="utf-8")
                    owner_rows = "owner.md wl:2\n"
                if duplicate:
                    (root / "other.md").write_text(task_frontmatter() + "other\n", encoding="utf-8")
                    owner_rows += "other.md wl:2\n"
                todo = root / "TODO.md"
                original_todo = f"current:\n{owner_rows}\nhuman pending:\nstale.md wl:2\n"
                todo.write_text(original_todo, encoding="utf-8")

                with redirect_stderr(io.StringIO()):
                    exit_code = run(StatusArgs(root, Path("stale.md"), "", "", stale_target="wl:2", retire_blocked_target=True))

                self.assertEqual(2, exit_code)
                self.assertEqual(original_task, path.read_text(encoding="utf-8"))
                self.assertEqual(original_todo, todo.read_text(encoding="utf-8"))

    def test_long_running_is_a_normal_status_transition(self) -> None:
        text = task_frontmatter() + "body\n"

        with patch("omo_manager.omo_task_status.frontmatter_parts", wraps=frontmatter_parts) as parse_parts:
            updated = update_frontmatter_status(text, "long_running", "persistent contact")

        parse_parts.assert_called_once_with(text)
        self.assertIn("status: long_running\nblocked_on: persistent contact\nrunat:", updated)
        self.assertIn("body\n", updated)

    def test_long_running_allows_missing_blocked_on(self) -> None:
        updated = update_frontmatter_status(task_frontmatter() + "body\n", "long_running", "")

        self.assertIn("status: long_running\nrunat:", updated)
        self.assertNotIn("blocked_on:", updated)

    def test_v2_dependency_blocked_long_running_preserves_resume_reason(self) -> None:
        updated = update_frontmatter_status(v2_task(), "long_running", "persistent contact")

        metadata = parse_task_metadata(updated)

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual("blocked", metadata.status)
        self.assertEqual("long_running", metadata.resume_status)
        self.assertIn("persistent contact", metadata.blocked_on)

        frontmatter, body = split_task_text(updated)
        values = load_yaml_mapping(frontmatter)
        values["pending_task_items"][0]["blocked_on"] = []
        self.assertTrue(sync_generated_blocker(values))
        resumed = parse_task_metadata(render_task(values, body))
        self.assertIsNotNone(resumed)
        assert resumed is not None
        self.assertEqual("long_running", resumed.status)
        self.assertEqual("persistent contact", resumed.blocked_on)

    def test_cli_v2_dependency_blocked_long_running_remains_a_normal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(v2_task(), encoding="utf-8")
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.blocking_request", return_value={"ok": True}):
                self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "long_running", "persistent contact")))

            metadata = parse_task_metadata(path.read_text(encoding="utf-8"), root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("blocked", metadata.status)
            self.assertEqual("long_running", metadata.resume_status)
            self.assertIn("persistent contact", metadata.blocked_on)

    def test_pending_marker_blocks_status_change(self) -> None:
        text = task_frontmatter() + "(pending)\nplease route\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "pending"):
            update_frontmatter_status(text, "done", "")

    def test_inline_pending_text_does_not_block_status_change(self) -> None:
        text = task_frontmatter() + "removed `(pending)` after recording it\n"

        updated = update_frontmatter_status(text, "done", "")

        self.assertIn("status: done\nrunat:", updated)

    def test_quoted_pending_line_does_not_block_status_change(self) -> None:
        text = task_frontmatter() + "> (pending)\nquoted old context\n"

        updated = update_frontmatter_status(text, "done", "")

        self.assertIn("status: done\nrunat:", updated)

    def test_done_rejects_pending_task_items(self) -> None:
        text = task_frontmatter(pending_items=("finish review",)) + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "verify each pending item is actually complete"):
            update_frontmatter_status(text, "done", "")

    def test_pending_marker_inside_fenced_code_is_allowed(self) -> None:
        text = task_frontmatter() + "```text\n(pending)\n```\n"

        updated = update_frontmatter_status(text, "done", "")

        self.assertIn("status: done\nrunat:", updated)

    def test_blocked_on_rejects_newline(self) -> None:
        text = task_frontmatter() + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "one line"):
            update_frontmatter_status(text, "blocked", "line one\ninjected: value")

    def test_blocked_requires_blocked_on(self) -> None:
        text = task_frontmatter() + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "required"):
            update_frontmatter_status(text, "blocked", "")

    def test_non_blocked_status_rejects_blocked_on(self) -> None:
        text = task_frontmatter(status="blocked", blocked_on="waiting") + "body\n"

        with self.assertRaisesRegex(TaskFrontmatterError, "only valid"):
            update_frontmatter_status(text, "running", "still waiting")

    def test_malformed_frontmatter_is_rejected(self) -> None:
        text = "---\nstatus: impossible\n---\nbody\n"

        with self.assertRaises(TaskFrontmatterError):
            update_frontmatter_status(text, "done", "")

    def test_cli_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            stdout = io.StringIO()
            close_calls: list[tuple[StopArgs, str]] = []

            def fake_record(args: StopArgs, session_id: str) -> None:
                close_calls.append((args, session_id))

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=fake_record,
            ), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(0, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))
            self.assertEqual(1, len(close_calls))
            self.assertEqual("task.md", close_calls[0][0].task_file)
            self.assertEqual("session-1", close_calls[0][1])
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())
            self.assertIn(DONE_REMINDER, stdout.getvalue())
            self.assertNotIn("email the human", stdout.getvalue().lower())

    def test_automatic_done_email_excludes_recovery_and_transfer_modes(self) -> None:
        from omo_manager.omo_task_status import automatic_done_email_eligible

        base = StatusArgs(Path("/tmp/root"), Path("task.md"), "done", "")
        self.assertTrue(automatic_done_email_eligible(base, "running"))
        self.assertFalse(automatic_done_email_eligible(base, "done"))
        for field in (
            "finish_closed_done",
            "finish_replaced_done",
            "recover_exited_shell_done",
            "close_shared_target",
            "close_retired_done",
            "close_missing_target",
        ):
            with self.subTest(field=field):
                self.assertFalse(automatic_done_email_eligible(replace(base, **{field: True}), "running"))

    def test_manager_done_queues_once_and_waits_for_exact_owner_delivery(self) -> None:
        from omo_manager.omo_task_status import require_owner_done_email

        args = StatusArgs(Path("/work"), Path("task.md"), "done", "")
        with patch("omo_manager.omo_task_status.require_owner_completion", return_value=False) as require:
            self.assertFalse(require_owner_done_email(args, Path("/work/task.md"), "task"))
        require.assert_called_once_with(Path("/work"), Path("/work/task.md"), "task", "task done")

    def test_manager_done_proceeds_only_after_delivery_marker(self) -> None:
        from omo_manager.omo_task_status import require_owner_done_email

        with patch("omo_manager.omo_task_status.require_owner_completion", return_value=True):
            self.assertTrue(require_owner_done_email(StatusArgs(Path("/work"), Path("task.md"), "done", ""), Path("/work/task.md"), "task"))

    def test_manager_done_owner_callback_then_retry_closes_with_one_email(self) -> None:
        from omo_manager.omo_completion_email import main as completion_main
        from omo_manager.omo_completion_email import require_owner_completion as actual_require

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            task = root / "task.md"
            manager = root / "manager.md"
            original = task_frontmatter() + "body\n"
            task.write_text(original, encoding="utf-8")
            manager.write_text(task_frontmatter(runat="wl:1", managerat="main:0", is_manager=True), encoding="utf-8")
            close_args = StopArgs("wl:2", 10.0, 2000, False, False, root, "task.md", True, 0.0)
            status_args = StatusArgs(root, Path("task.md"), "done", "")
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(state)}), patch(
                "omo_manager.omo_task_status.require_owner_completion", side_effect=actual_require
            ), patch("omo_manager.omo_completion_email.current_active_task", return_value=manager), patch(
                "omo_manager.omo_tmux_send.send_system_to_codex"
            ) as queue, patch("omo_manager.omo_task_status.stop_done_agent", return_value=(close_args, "session-1")) as stop, patch(
                "omo_manager.omo_task_status.record_close"
            ), redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(status_args))
                self.assertEqual(original, task.read_text(encoding="utf-8"))
                stop.assert_not_called()
                owner_command = queue.call_args.args[1].splitlines()[-1]
                self.assertIn("omo_completion_email.py", owner_command)
                with patch("omo_manager.omo_completion_email.current_active_task", return_value=task), patch(
                    "omo_manager.omo_completion_email.subprocess.run"
                ) as email:
                    self.assertEqual(0, completion_main(["--root", str(root), "--task", str(task), "--outcome", "task done"]))
                    self.assertEqual(0, run(status_args))
                email.assert_called_once()
            queue.assert_called_once()
            stop.assert_called_once()
            self.assertIn("status: done\n", task.read_text(encoding="utf-8"))

    def test_manager_done_closes_from_cross_state_reconciled_receipt(self) -> None:
        from omo_manager.omo_completion_email import build_completion_email
        from omo_manager.omo_completion_email import claim_completion_email
        from omo_manager.omo_completion_email import mark_completion_email_delivered
        from omo_manager.omo_completion_email import reconcile_delivered_completion
        from omo_manager.omo_completion_email import require_owner_completion as actual_require

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner_state = root / "owner-state"
            manager_state = root / "manager-state"
            task = root / "task.md"
            manager = root / "manager.md"
            original = task_frontmatter(status="blocked", blocked_on="waiting") + "body\n"
            task.write_text(original, encoding="utf-8")
            manager_state.mkdir(mode=0o700)
            manager.write_text(task_frontmatter(runat="wl:1", managerat="main:0", is_manager=True), encoding="utf-8")
            plan = build_completion_email(root, task, original, "task done")
            assert plan is not None
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(owner_state)}):
                self.assertTrue(claim_completion_email(plan))
                mark_completion_email_delivered(plan)
            receipt = owner_state / "completion-email-delivered" / plan.key
            with patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": str(manager_state)}):
                reconcile_delivered_completion(
                    root,
                    task,
                    "task done",
                    "wl:2",
                    hashlib.sha256(original.encode()).hexdigest(),
                    receipt,
                    hashlib.sha256(receipt.read_bytes()).hexdigest(),
                )
                close_args = StopArgs("wl:2", 10.0, 2000, False, False, root, "task.md", True, 0.0)
                with patch("omo_manager.omo_task_status.require_owner_completion", side_effect=actual_require), patch(
                    "omo_manager.omo_completion_email.current_active_task", return_value=manager
                ), patch("omo_manager.omo_tmux_send.send_system_to_codex") as queue, patch(
                    "omo_manager.omo_task_status.stop_done_agent", return_value=(close_args, "session-1")
                ), patch("omo_manager.omo_task_status.record_close"):
                    self.assertEqual(0, run(StatusArgs(root, Path("task.md"), "done", "")))
            queue.assert_not_called()
            self.assertIn("status: done\n", task.read_text(encoding="utf-8"))

    def test_cli_done_rejects_manager_with_active_child_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(runat="vl:2", managerat="vl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(task_frontmatter(runat="vl:3", managerat="vl:2.0") + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager with children")) as stop_agent, redirect_stderr(stderr):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            stop_agent.assert_not_called()
            self.assertIn("manager task still owns active child task(s): child.md", stderr.getvalue())
            self.assertIn("--migrate-manager-owner --old-manager-target vl:2 --new-manager-target vl:1", stderr.getvalue())

    def test_cli_done_allows_manager_with_done_child_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            notes = root / "notes.md"
            manager.write_text(task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True) + "body\n", encoding="utf-8")
            child.write_text(task_frontmatter(status="done", runat="retired", managerat="wl:2.0") + "body\n", encoding="utf-8")
            child_before = child.read_bytes()
            notes.write_text("---\nversion: article\nstatus: draft\n---\nnotes\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, root, "manager.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close"
            ), redirect_stdout(stdout):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(0, exit_code)
            self.assertEqual(child_before, child.read_bytes())
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())

    def test_cli_done_rejects_manager_with_active_retired_child_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(task_frontmatter(runat="retired", managerat="wl:2.0") + "body\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager with invalid child")) as stop_agent, redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            stop_agent.assert_not_called()

    def test_cli_done_rejects_manager_when_child_ownership_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            child = root / "child.md"
            original = task_frontmatter(runat="wl:2", managerat="wl:1", is_manager=True) + "body\n"
            manager.write_text(original, encoding="utf-8")
            child.write_text(
                "\n".join(
                    [
                        "---",
                        "version: v1.0.0",
                        "status: running",
                        "runat: wl:3",
                        "tool: codex",
                        "managerat: wl:9",
                        "managerat: wl:2",
                        "is_manager: false",
                        "unexpected: bad",
                        "pending_task_items: []",
                        "---",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not close manager when ownership is uncertain")) as stop_agent, redirect_stderr(stderr):
                exit_code = run(StatusArgs(root, Path("manager.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))
            stop_agent.assert_not_called()
            self.assertIn("cannot verify manager child ownership because `child.md` has invalid task frontmatter", stderr.getvalue())

    def replacement_args(self, root: Path, **changes: object) -> StatusArgs:
        stale = root / "stale.md"
        replacement = root / "replacement.md"
        values: dict[str, object] = {
            "root": root,
            "task_file": Path("stale.md"),
            "status": "done",
            "blocked_on": "",
            "finish_replaced_done": True,
            "replacement_task": replacement,
            "stale_target": "old:2",
            "replacement_target": "new:3",
            "stale_sha256": hashlib.sha256(stale.read_bytes()).hexdigest(),
            "replacement_sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
            "replacement_status": "long_running",
            "protected_targets": ("protected:8",),
            "stopped_evidence": "verified stopped legacy target",
            "replacement_pane_evidence": "successor is active",
            "audit_output": root / "replacement.audit",
        }
        values.update(changes)
        return StatusArgs(**values)  # type: ignore[arg-type]

    def write_replacement_tasks(
        self,
        root: Path,
        *,
        stale_pending: tuple[str, ...] = (),
        replacement_managerat: str = "owner:1",
        replacement_is_manager: bool = True,
    ) -> tuple[Path, Path, str, str]:
        evidence = "verified stopped legacy target"
        stale_text = task_frontmatter(status="blocked", blocked_on="replaced", pending_items=stale_pending, runat="old:2", managerat="owner:1", is_manager=True) + f"(verified empty stale task: {evidence})\n"
        replacement_text = task_frontmatter(status="long_running", pending_items=("finish authoritative work",), runat="new:3", managerat=replacement_managerat, is_manager=replacement_is_manager)
        stale = root / "stale.md"
        replacement = root / "replacement.md"
        stale.write_text(stale_text, encoding="utf-8")
        replacement.write_text(replacement_text, encoding="utf-8")
        (root / "TODO.md").write_text("current:\nstale.md old:2\nreplacement.md new:3\n\nprevious:\n", encoding="utf-8")
        return stale, replacement, stale_text, replacement_text

    def test_finish_replaced_done_accepts_different_live_successor_and_writes_private_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale, replacement, _stale_text, replacement_text = self.write_replacement_tasks(root)
            args = self.replacement_args(root)

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                patch("omo_manager.omo_task_status.capture", return_value="successor is active") as capture_call,
                patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("must not signal either pane")),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, run(args))

            self.assertIn("status: done\nrunat: old:2", stale.read_text(encoding="utf-8"))
            self.assertEqual(replacement_text, replacement.read_text(encoding="utf-8"))
            self.assertEqual("current:\nreplacement.md new:3\n\nprevious:\nstale.md old:2\n", (root / "TODO.md").read_text(encoding="utf-8"))
            self.assertEqual(2, capture_call.call_count)
            capture_call.assert_called_with("%3", 2000)
            audit = root / "replacement.audit"
            self.assertEqual(0o600, audit.stat().st_mode & 0o777)
            audit_text = audit.read_text(encoding="utf-8")
            self.assertIn("replacement-pane-id: %3\n", audit_text)
            self.assertIn(f"stopped-evidence-sha256: {hashlib.sha256(args.stopped_evidence.encode()).hexdigest()}\n", audit_text)
            self.assertIn(f"replacement-pane-evidence-sha256: {hashlib.sha256(args.replacement_pane_evidence.encode()).hexdigest()}\n", audit_text)
            self.assertIn("completion: unknown-until-finalized\n", audit_text)
            self.assertIn("final-result: success\n", audit_text)

    def test_finish_replaced_done_refuses_mismatched_task_target_and_nonempty_stale_queue(self) -> None:
        for case in ("stale target", "successor target", "queue"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root, stale_pending=("still open",) if case == "queue" else ())
                args = self.replacement_args(root, stale_target="wrong:2") if case == "stale target" else self.replacement_args(root, replacement_target="wrong:3") if case == "successor target" else self.replacement_args(root)
                with patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertFalse((root / "replacement.audit").exists())
                capture_call.assert_not_called()

    def test_finish_replaced_done_refuses_changed_lifecycle_bytes_and_missing_successor_pane(self) -> None:
        for case in ("bytes", "pane"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, replacement, stale_text, replacement_text = self.write_replacement_tasks(root)
                args = self.replacement_args(root)

                def capture(_pane: str, _lines: int) -> str:
                    replacement.write_text(replacement_text + "changed lifecycle bytes\n", encoding="utf-8")
                    return "successor is active"

                pane = (lambda target: "" if target == "old:2" else "") if case == "pane" else (lambda target: "" if target == "old:2" else "%3")
                capture_side_effect = capture if case == "bytes" else None
                with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane), patch("omo_manager.omo_task_status.capture", side_effect=capture_side_effect) as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertFalse((root / "replacement.audit").exists())
                if case == "pane":
                    capture_call.assert_not_called()

    def test_finish_replaced_done_revalidates_live_targets_after_audit_reservation(self) -> None:
        for case in ("stale", "successor pane", "successor evidence"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
                reserved = False

                def reserve(path: Path, text: str) -> None:
                    nonlocal reserved
                    reserve_private_audit(path, text)
                    reserved = True

                def pane_id(target: str) -> str:
                    if target == "old:2":
                        return "%2" if reserved and case == "stale" else ""
                    return "%9" if reserved and case == "successor pane" else "%3"

                def capture(_pane: str, _lines: int) -> str:
                    return "evidence changed" if reserved and case == "successor evidence" else "successor is active"

                with (
                    patch("omo_manager.omo_task_status.reserve_private_audit", side_effect=reserve),
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=pane_id),
                    patch("omo_manager.omo_task_status.capture", side_effect=capture),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(self.replacement_args(root)))

                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertIn("final-result: not-completed", (root / "replacement.audit").read_text(encoding="utf-8"))

    def test_finish_replaced_done_refuses_manager_owner_or_role_mismatch(self) -> None:
        for case in ("owner", "role"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(
                    root,
                    replacement_managerat="other:1" if case == "owner" else "owner:1",
                    replacement_is_manager=case != "role",
                )
                args = self.replacement_args(root)
                with patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"), patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                capture_call.assert_not_called()

    def test_finish_replaced_done_refuses_duplicate_or_invalid_competing_successor_owner(self) -> None:
        for case in ("duplicate", "invalid"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
                competitor = root / "competitor.md"
                competitor.write_text(
                    task_frontmatter(status="running", pending_items=("competing work",), runat="new:3", managerat="owner:1", is_manager=True)
                    if case == "duplicate"
                    else task_frontmatter(status="running", pending_items=("competing work",), runat="new:3", managerat="owner:1", is_manager=True).replace("runat: new:3\n", "runat: new:3\nrunat: new:3\n"),
                    encoding="utf-8",
                )
                with (
                    patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                    patch("omo_manager.omo_task_status.capture") as capture_call,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(2, run(self.replacement_args(root)))
                self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
                self.assertFalse((root / "replacement.audit").exists())
                capture_call.assert_not_called()

    def test_finish_replaced_done_refuses_human_or_explicitly_protected_target_before_capture(self) -> None:
        for case in ("human", "protected"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
                if case == "human":
                    stale.write_text(stale_text.replace("runat: old:2", "runat: hlegacy:2"), encoding="utf-8")
                    args = self.replacement_args(
                        root,
                        stale_target="hlegacy:2",
                        stale_sha256=hashlib.sha256(stale.read_bytes()).hexdigest(),
                    )
                else:
                    args = self.replacement_args(root, protected_targets=("new:3.0",))
                with patch("omo_manager.omo_task_status.exact_pane_id") as pane_call, patch("omo_manager.omo_task_status.capture") as capture_call, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                pane_call.assert_not_called()
                capture_call.assert_not_called()
                self.assertFalse((root / "replacement.audit").exists())

    def test_finish_replaced_done_leaves_durable_unknown_audit_if_success_finalization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale, _replacement, _stale_text, _replacement_text = self.write_replacement_tasks(root)
            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                patch("omo_manager.omo_task_status.capture", return_value="successor is active"),
                patch("omo_manager.omo_task_status.finish_private_audit", side_effect=TaskFrontmatterError("audit storage failed")),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(self.replacement_args(root)))
            self.assertIn("status: done\nrunat: old:2", stale.read_text(encoding="utf-8"))
            audit_text = (root / "replacement.audit").read_text(encoding="utf-8")
            self.assertIn("completion: unknown-until-finalized", audit_text)
            self.assertNotIn("final-result:", audit_text)

    def test_finish_replaced_done_rolls_back_todo_when_task_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale, _replacement, stale_text, _replacement_text = self.write_replacement_tasks(root)
            todo = root / "TODO.md"
            todo_text = todo.read_text(encoding="utf-8")
            args = self.replacement_args(root)

            def fail_task_replace(target: Path, text: str, before: object) -> None:
                if target == stale:
                    raise OSError("task write failed")
                replace_if_unchanged_locked(target, text, before)  # type: ignore[arg-type]

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", side_effect=lambda target: "" if target == "old:2" else "%3"),
                patch("omo_manager.omo_task_status.capture", return_value="successor is active"),
                patch("omo_manager.omo_task_status.replace_if_unchanged_locked", side_effect=fail_task_replace),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(2, run(args))

            self.assertEqual(stale_text, stale.read_text(encoding="utf-8"))
            self.assertEqual(todo_text, todo.read_text(encoding="utf-8"))
            self.assertIn("final-result: not-completed", (root / "replacement.audit").read_text(encoding="utf-8"))

    def test_cli_done_failure_marks_blocked_when_close_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter() + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=RuntimeError("tmux target not found")), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertNotEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("status: blocked\nblocked_on: done_close_failed: tmux target not found\n", path.read_text(encoding="utf-8"))
            self.assertIn("failed to close done agent", stderr.getvalue())

    def test_cli_done_marks_blocked_when_close_bookkeeping_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=RuntimeError("TODO locked"),
            ), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: blocked\nblocked_on: done_close_bookkeeping_failed: TODO locked\n", text)
            self.assertIn("done close bookkeeping failed", stderr.getvalue())

    def test_cli_done_bookkeeping_failure_normalizes_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter() + "body\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.record_close",
                side_effect=RuntimeError("line one\nline two"),
            ), redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            self.assertIn("status: blocked\nblocked_on: done_close_bookkeeping_failed: line one line two\n", path.read_text(encoding="utf-8"))

    def test_cli_done_write_failure_after_close_leaves_blocked_in_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter() + "body\n"
            path.write_text(original, encoding="utf-8")

            def flaky_replace(target: Path, text: str, before: object) -> None:
                if "status: done" in text:
                    raise RuntimeError("write raced")
                target.write_text(text, encoding="utf-8")

            with patch("omo_manager.omo_task_status.stop_done_agent", return_value=(StopArgs("wl:2", 10.0, 2000, False, False, Path(tmp), "task.md", True, 0.0), "session-1")), patch(
                "omo_manager.omo_task_status.replace_if_unchanged",
                side_effect=flaky_replace,
            ), redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", ""))

            self.assertEqual(2, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertNotEqual(original, text)
            self.assertIn("status: blocked\nblocked_on: done_close_in_progress: manager is closing the agent before marking done\n", text)
            self.assertIn("session_id: `session-1`", text)
            self.assertNotIn("status: done", text)

    def test_cli_finish_closed_done_records_bookkeeping_without_closing_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failed: TODO locked") + "body\n", encoding="utf-8")
            stdout = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("should not stop twice")), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat:", text)
            self.assertNotIn("blocked_on:", text)
            self.assertIn("session_id: `session-1`", text)
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())
            self.assertIn(DONE_REMINDER, stdout.getvalue())

    def test_cli_recover_exited_shell_done_closes_and_finishes_exact_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = "%42"
            session_id = "11111111-2222-3333-4444-555555555555"
            path = root / "task.md"
            blocker = f"done_close_failed: target is not a supported live Codex pane: {pane} status=not_codex"
            path.write_text(task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1") + "body\n", encoding="utf-8")
            todo = root / "TODO.md"
            todo.write_text("current:\ntask.md cfg:1\n\nprevious:\nother.md\n", encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                session_id=session_id,
                recover_exited_shell_done=True,
                pane_id=pane,
                terminal_evidence="accepted-report-token",
            )

            with (
                patch("omo_manager.omo_task_status.exact_pane_id", return_value=pane),
                patch("omo_manager.omo_task_status.close_exited_codex_shell") as close,
                redirect_stdout(io.StringIO()),
            ):
                exit_code = run(args)

            self.assertEqual(0, exit_code)
            close.assert_called_once_with("cfg:1", pane, session_id, "accepted-report-token")
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat: cfg:1", text)
            self.assertIn(f"session_id: `{session_id}`", text)
            self.assertEqual("current:\n\nprevious:\ntask.md cfg:1\nother.md\n", todo.read_text(encoding="utf-8"))

    def test_cli_recover_exited_shell_done_rejects_unsafe_task_or_index(self) -> None:
        pane = "%42"
        session_id = "11111111-2222-3333-4444-555555555555"
        blocker = f"done_close_failed: target is not a supported live Codex pane: {pane} status=not_codex"
        cases = (
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1", is_manager=True), "task.md cfg:1", None),
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1", pending_items=("open",)), "task.md cfg:1", None),
            (task_frontmatter(status="blocked", blocked_on="done_close_failed: another failure", runat="cfg:1"), "task.md cfg:1", None),
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1"), "task.md (done)", None),
            (task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1"), "task.md cfg:1", task_frontmatter(status="running", runat="cfg:1")),
        )
        for task_text, todo_row, reused_text in cases:
            with self.subTest(todo_row=todo_row, reused=bool(reused_text)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "task.md"
                original = task_text + "body\n"
                path.write_text(original, encoding="utf-8")
                todo = root / "TODO.md"
                todo_original = f"current:\n{todo_row}\n\nprevious:\n"
                todo.write_text(todo_original, encoding="utf-8")
                if reused_text is not None:
                    (root / "reused.md").write_text(reused_text + "body\n", encoding="utf-8")
                args = StatusArgs(
                    root,
                    Path("task.md"),
                    "done",
                    "",
                    session_id=session_id,
                    recover_exited_shell_done=True,
                    pane_id=pane,
                    terminal_evidence="accepted-report-token",
                )
                with patch("omo_manager.omo_task_status.close_exited_codex_shell") as close, redirect_stderr(io.StringIO()):
                    self.assertEqual(2, run(args))
                close.assert_not_called()
                self.assertEqual(original, path.read_text(encoding="utf-8"))
                self.assertEqual(todo_original, todo.read_text(encoding="utf-8"))

    def test_cli_recover_exited_shell_done_rejects_human_pending_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = "%42"
            session_id = "11111111-2222-3333-4444-555555555555"
            blocker = f"done_close_failed: target is not a supported live Codex pane: {pane} status=not_codex"
            path = root / "task.md"
            original = task_frontmatter(status="blocked", blocked_on=blocker, runat="cfg:1") + "body\n"
            path.write_text(original, encoding="utf-8")
            todo = root / "TODO.md"
            todo_original = "current:\n\nhuman pending:\ntask.md cfg:1\n\nprevious:\n"
            todo.write_text(todo_original, encoding="utf-8")
            args = StatusArgs(
                root,
                Path("task.md"),
                "done",
                "",
                session_id=session_id,
                recover_exited_shell_done=True,
                pane_id=pane,
                terminal_evidence="accepted-report-token",
            )

            with patch("omo_manager.omo_task_status.close_exited_codex_shell") as close, redirect_stderr(io.StringIO()):
                self.assertEqual(2, run(args))

            close.assert_not_called()
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertEqual(todo_original, todo.read_text(encoding="utf-8"))

    def test_parse_recover_exited_shell_done_requires_explicit_evidence(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--recover-exited-shell-done", "task.md"])

        args = parse_args(
            [
                "--root",
                "/tmp/work",
                "--recover-exited-shell-done",
                "--pane-id",
                "%42",
                "--session-id",
                "11111111-2222-3333-4444-555555555555",
                "--terminal-evidence",
                "accepted-report-token",
                "task.md",
            ]
        )
        self.assertTrue(args.recover_exited_shell_done)
        self.assertEqual("%42", args.pane_id)

    def test_cli_finish_closed_done_failure_stays_blocked_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failed: TODO locked") + "body\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task_status.finish_done_transaction", side_effect=RuntimeError("TODO still locked")), redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: blocked\nblocked_on: done_close_bookkeeping_failed: TODO still locked\n", text)
            self.assertIn("done close bookkeeping retry failed", stderr.getvalue())

    def test_cli_finish_closed_done_allows_close_in_progress_with_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="done_close_in_progress: manager is closing the agent before marking done")
                + "body\n"
                + "(manager closed Codex agent 07-14 11:00 PDT; tmux target `wl:2`; session_id: `session-1`.)\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("should not stop twice")), redirect_stdout(stdout):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat:", text)
            self.assertNotIn("blocked_on:", text)
            self.assertIn("Closed wl:2; session_id: session-1.", stdout.getvalue())

    def test_cli_finish_closed_done_accepts_close_failed_with_matching_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="done_close_failed: refusing to stop the current pane: wl:2")
                + "(manager closed Codex agent 07-15 00:19 PDT; tmux target `wl:2`; session_id: `session-1`.)\n",
                encoding="utf-8",
            )

            with patch("omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("already closed")), redirect_stdout(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(0, exit_code)
            self.assertIn("status: done\nrunat:", path.read_text(encoding="utf-8"))

    def test_cli_finish_closed_done_accepts_missing_worker_already_in_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(
                task_frontmatter(status="blocked", blocked_on="done_close_failed: target disappeared during status query") + "body\n",
                encoding="utf-8",
            )
            (root / "TODO.md").write_text("current:\n\nprevious:\ntask.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), patch(
                "omo_manager.omo_task_status.stop_done_agent", side_effect=AssertionError("already closed")
            ), redirect_stdout(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("task.md"), "done", "", True, "unverified-session"))

            self.assertEqual(0, exit_code)
            text = path.read_text(encoding="utf-8")
            self.assertIn("status: done\nrunat:", text)
            self.assertIn("Codex session id not found in captured tmux output", text)
            self.assertNotIn("unverified-session", text)

    def test_cli_finish_closed_done_does_not_trust_missing_manager_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_failed: target disappeared", is_manager=True) + "body\n"
            path.write_text(original, encoding="utf-8")
            (root / "TODO.md").write_text("previous:\ntask.md wl:2\n", encoding="utf-8")

            with patch("omo_manager.omo_task_status.exact_pane_id", return_value=""), redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(root, Path("task.md"), "done", "", True, ""))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_cli_finish_closed_done_rejects_close_failed_with_wrong_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = (
                task_frontmatter(status="blocked", blocked_on="done_close_failed: refusing to stop the current pane: wl:2")
                + "(manager closed Codex agent 07-15 00:19 PDT; tmux target `wl:2`; session_id: `session-1`.)\n"
            )
            path.write_text(original, encoding="utf-8")

            with redirect_stderr(io.StringIO()):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "wrong-session"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_cli_finish_closed_done_rejects_pre_close_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_failed: tmux target not found") + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("requires failed close bookkeeping or a matching prior-close note", stderr.getvalue())

    def test_cli_finish_closed_done_rejects_invalid_bookkeeping_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_bookkeeping_failedly: forged") + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("requires failed close bookkeeping or a matching prior-close note", stderr.getvalue())

    def test_cli_finish_closed_done_rejects_close_in_progress_without_close_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.md"
            original = task_frontmatter(status="blocked", blocked_on="done_close_in_progress: manager is closing the agent before marking done") + "body\n"
            path.write_text(original, encoding="utf-8")
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("task.md"), "done", "", True, "session-1"))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, path.read_text(encoding="utf-8"))
            self.assertIn("requires failed close bookkeeping or a matching prior-close note", stderr.getvalue())

    def test_parse_finish_closed_done_without_status(self) -> None:
        args = parse_args(["--root", "/tmp/work", "--finish-closed-done", "--session-id", "session-1", "task.md"])

        self.assertEqual(Path("/tmp/work"), args.root)
        self.assertEqual(Path("task.md"), args.task_file)
        self.assertEqual("done", args.status)
        self.assertTrue(args.finish_closed_done)
        self.assertEqual("session-1", args.session_id)

    def test_parse_finish_replaced_done_requires_all_explicit_evidence(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parse_args(["--root", "/tmp/work", "--finish-replaced-done", "task.md"])

        args = parse_args(
            [
                "--root",
                "/tmp/work",
                "--finish-replaced-done",
                "--replacement-task",
                "/tmp/replacement.md",
                "--stale-target",
                "old:2",
                "--replacement-target",
                "new:3",
                "--stale-sha256",
                "a" * 64,
                "--replacement-sha256",
                "b" * 64,
                "--replacement-status",
                "long_running",
                "--protected-target",
                "protected:8",
                "--stopped-evidence",
                "verified stop",
                "--replacement-pane-evidence",
                "replacement output",
                "--audit-output",
                "/tmp/replacement.audit",
                "task.md",
            ]
        )

        self.assertTrue(args.finish_replaced_done)
        self.assertEqual(Path("/tmp/replacement.md"), args.replacement_task)
        self.assertEqual(("protected:8",), args.protected_targets)

    def test_cli_running_has_no_done_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "task.md"
            path.write_text(task_frontmatter(status="blocked", blocked_on="waiting") + "body\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\n\nhuman pending:\ntask.md wl:2\n", encoding="utf-8")
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = run(StatusArgs(root, Path("task.md"), "running", ""))

            self.assertEqual(0, exit_code)
            self.assertEqual("", stdout.getvalue())

    def test_cli_rejects_relative_path_that_escapes_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                exit_code = run(StatusArgs(Path(tmp), Path("../task.md"), "done", ""))

            self.assertEqual(2, exit_code)


if __name__ == "__main__":
    unittest.main()
