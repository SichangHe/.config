from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omo_manager.amh_problem_claim import claim_problem, read_claims
from omo_manager import omo_pending_watch as watcher


TASK_ID_A = "task_019f0000-0000-7000-8000-000000000011"
TASK_ID_B = "task_019f0000-0000-7000-8000-000000000012"


def task_frontmatter(
    status: str,
    target: str,
    owner_target: str,
    reason: str = "",
    *,
    task_id: str = "",
    is_manager: bool = False,
) -> str:
    if task_id:
        blocked = f"resume_status: running\nblocked_on:\n  - kind: human\n    reason: {reason}\n" if status == "blocked" else ""
        return (
            "---\n"
            "version: v2.0.0\n"
            f"task_id: {task_id}\n"
            f"status: {status}\n"
            f"{blocked}"
            f"runat: {target}\n"
            "tool: codex\n"
            f"managerat: {owner_target}\n"
            f"is_manager: {'true' if is_manager else 'false'}\n"
            "pending_task_items: []\n"
            "resolved_task_items: []\n"
            "---\n"
        )
    blocked = f"blocked_on: {reason}\n" if reason else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocked}"
        f"runat: {target}\n"
        "tool: codex\n"
        f"managerat: {owner_target}\n"
        f"is_manager: {'true' if is_manager else 'false'}\n"
        "pending_task_items: []\n"
        "---\n"
    )


def problem_line(
    task_file: str,
    target: str,
    owner_target: str,
    reason: str,
    *,
    problem_class: str = "blocked_idle",
    task_status: str = "blocked",
    idle_state: str = "ready",
) -> str:
    reason_field = f" reason={reason}" if reason else ""
    return (
        f"{problem_class}: task={task_file} evidence=target={target} role=blocked_idle "
        f"task_status={task_status} idle_status={idle_state}{reason_field} owner_target={owner_target}"
    )


def problem_result(*lines: str) -> watcher.CommandOutput:
    classes = " ".join(f"{name}={sum(line.startswith(f'{name}: ') for line in lines)}" for name in ("missing", "blocked_idle") if any(line.startswith(f"{name}: ") for line in lines))
    return watcher.CommandOutput("agent-problems", 3, f"agent-problems: {classes}\n" + "\n".join(lines) + "\n", "")


class WatcherProblemEpisodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "watcher-state" / "consumed.tsv"
        self.args = watcher.Args(
            self.root,
            "",
            self.state,
            1.0,
            1.0,
            30.0,
            self.root / "status.py",
            False,
            False,
            manager_target="mgr:1",
            agent_problem_repeat_s=300.0,
        )

    def write_task(
        self,
        task_file: str = "worker.md",
        target: str = "agent:2",
        owner_target: str = "mgr:1",
        reason: str = "waiting on approval",
        *,
        status: str = "blocked",
        task_id: str = "",
        todo_ref: str | None = None,
        extra_todo: tuple[tuple[str, str], ...] = (),
    ) -> None:
        reference = todo_ref or task_file
        todo = ["current:", f"{reference} {target}", *(f"{path} {pane}" for path, pane in extra_todo)]
        (self.root / "TODO.md").write_text("\n".join(todo) + "\n", encoding="utf-8")
        (self.root / task_file).write_text(task_frontmatter(status, target, owner_target, reason, task_id=task_id), encoding="utf-8")

    def handle(self, seen: dict[str, float], result: watcher.CommandOutput, now_s: float, push: MagicMock) -> bool:
        with patch.object(watcher, "agent_problem_target_is_ready", return_value=True), patch.object(
            watcher, "push_manager_text_to_target", push
        ), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("unexpected human contact")):
            return watcher.handle_agent_problem_result(self.args, seen, result, now_s, {})

    def test_first_exact_restart_changes_and_recurrence(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        blocked = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))
        push = MagicMock(return_value=0)
        seen: dict[str, float] = {}

        self.assertTrue(self.handle(seen, blocked, 1000.0, push))
        self.assertFalse(self.handle({}, blocked, 1001.0, push))
        self.assertEqual(1, push.call_count)

        changed_idle = problem_result(
            problem_line("worker.md", "agent:2", "mgr:1", reason, idle_state="blocked_idle")
        )
        self.assertTrue(self.handle({}, changed_idle, 1001.5, push))

        changed_observation = problem_result(
            problem_line("worker.md", "agent:2", "mgr:1", reason, idle_state="blocked_idle").replace(
                "role=blocked_idle", "role=blocked_wait"
            )
        )
        self.assertTrue(self.handle({}, changed_observation, 1001.75, push))

        missing = problem_result(
            problem_line("worker.md", "agent:2", "mgr:1", reason, problem_class="missing", idle_state="missing")
        )
        self.assertTrue(self.handle(seen, missing, 1002.0, push))
        self.assertFalse(self.handle({}, missing, 1003.0, push))

        changed_reason = "waiting on replacement approval"
        self.write_task(reason=changed_reason)
        changed = problem_result(
            problem_line("worker.md", "agent:2", "mgr:1", changed_reason, problem_class="missing", idle_state="missing")
        )
        self.assertTrue(self.handle(seen, changed, 1004.0, push))

        self.write_task(target="agent:3", owner_target="mgr:1", reason=changed_reason)
        retargeted = problem_result(
            problem_line("worker.md", "agent:3", "mgr:1", changed_reason, problem_class="missing", idle_state="missing")
        )
        self.assertTrue(self.handle(seen, retargeted, 1005.0, push))

        self.write_task(target="agent:3", owner_target="mgr:2", reason=changed_reason)
        reowned = problem_result(
            problem_line("worker.md", "agent:3", "mgr:2", changed_reason, problem_class="missing", idle_state="missing")
        )
        self.assertTrue(self.handle(seen, reowned, 1005.5, push))

        self.write_task(target="agent:3", owner_target="mgr:2", reason="", status="running")
        changed_lifecycle = problem_result(
            problem_line("worker.md", "agent:3", "mgr:2", "", problem_class="missing", task_status="running", idle_state="missing")
        )
        self.assertTrue(self.handle(seen, changed_lifecycle, 1006.0, push))

        healthy = watcher.CommandOutput("agent-problems", 0, "", "")
        self.assertFalse(self.handle(seen, healthy, 1007.0, push))
        self.write_task(target="agent:3", owner_target="mgr:2", reason="", status="running")
        self.assertTrue(self.handle(seen, changed_lifecycle, 1008.0, push))
        self.assertEqual(9, push.call_count)

    def test_stale_absent_alias_and_shared_target_never_suppress(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        initial = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))
        push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, initial, 1000.0, push))

        stale = problem_result(problem_line("worker.md", "agent:9", "mgr:1", reason))
        self.assertTrue(self.handle({}, stale, 1001.0, push))
        self.assertTrue(self.handle({}, stale, 1002.0, push))

        (self.root / "TODO.md").write_text("current:\n", encoding="utf-8")
        self.assertTrue(self.handle({}, initial, 1003.0, push))
        self.assertTrue(self.handle({}, initial, 1004.0, push))

        self.write_task(reason=reason, todo_ref="./worker.md")
        alias = problem_result(problem_line("./worker.md", "agent:2", "mgr:1", reason))
        self.assertTrue(self.handle({}, alias, 1005.0, push))
        self.assertTrue(self.handle({}, alias, 1006.0, push))

        self.write_task(reason=reason, extra_todo=(("peer.md", "agent:2.0"),))
        (self.root / "peer.md").write_text(task_frontmatter("running", "agent:2.0", "mgr:1"), encoding="utf-8")
        self.assertTrue(self.handle({}, initial, 1007.0, push))
        self.assertTrue(self.handle({}, initial, 1008.0, push))
        self.assertEqual(9, push.call_count)

    def test_absent_bound_fields_and_duplicate_v2_identity_never_suppress(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        complete = problem_line("worker.md", "agent:2", "mgr:1", reason)
        variants = (
            complete.replace(" owner_target=mgr:1", ""),
            complete.replace(" task_status=blocked", ""),
            complete.replace(" idle_status=ready", ""),
            complete.replace(f" reason={reason}", ""),
        )
        push = MagicMock(return_value=0)
        now_s = 1000.0
        for line in variants:
            with self.subTest(line=line):
                self.assertTrue(self.handle({}, problem_result(line), now_s, push))
                self.assertTrue(self.handle({}, problem_result(line), now_s + 0.5, push))
            now_s += 1.0

        self.write_task(reason=reason, task_id=TASK_ID_A, extra_todo=(("peer.md", "agent:3"),))
        (self.root / "peer.md").write_text(
            task_frontmatter("running", "agent:3", "mgr:1", task_id=TASK_ID_A),
            encoding="utf-8",
        )
        duplicate_id = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))
        self.assertTrue(self.handle({}, duplicate_id, 1010.0, push))
        self.assertTrue(self.handle({}, duplicate_id, 1011.0, push))
        self.assertEqual(10, push.call_count)

    def test_absent_or_ambiguous_dependency_authority_never_suppresses(self) -> None:
        (self.root / "TODO.md").write_text("current:\nmanager.md agent:2\nleaf.md agent:3\n", encoding="utf-8")
        (self.root / "manager.md").write_text(
            task_frontmatter("blocked", "agent:2", "mgr:1", "leaf.md", is_manager=True),
            encoding="utf-8",
        )
        (self.root / "leaf.md").write_text(
            task_frontmatter("running", "agent:3", "agent:2"),
            encoding="utf-8",
        )
        blocked = problem_result(problem_line("manager.md", "agent:2", "mgr:1", "leaf.md"))
        push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, blocked, 1000.0, push))
        self.assertFalse(self.handle({}, blocked, 1001.0, push))

        (self.root / "TODO.md").write_text("current:\nmanager.md agent:2\n", encoding="utf-8")
        self.assertTrue(self.handle({}, blocked, 1002.0, push))
        self.assertTrue(self.handle({}, blocked, 1003.0, push))

        (self.root / "TODO.md").write_text("current:\nmanager.md agent:2\nleaf.md agent:3\n", encoding="utf-8")
        self.assertTrue(self.handle({}, blocked, 1004.0, push))
        self.assertEqual(4, push.call_count)

    def test_canonical_task_path_and_frontmatter_version_changes_realert(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        initial = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))
        push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, initial, 1000.0, push))

        self.write_task(reason=reason, task_id=TASK_ID_A)
        self.assertTrue(self.handle({}, initial, 1001.0, push))

        (self.root / "worker.md").rename(self.root / "replacement.md")
        (self.root / "TODO.md").write_text("current:\nreplacement.md agent:2\n", encoding="utf-8")
        replacement = problem_result(problem_line("replacement.md", "agent:2", "mgr:1", reason))
        self.assertTrue(self.handle({}, replacement, 1002.0, push))
        self.assertEqual(3, push.call_count)

    def test_running_missing_episode_without_idle_field_suppresses_and_recurs(self) -> None:
        self.write_task(reason="", status="running")
        missing_line = (
            "missing: task=worker.md evidence=target=agent:2 task_status=running owner_target=mgr:1"
        )
        missing = problem_result(missing_line)
        push = MagicMock(return_value=0)

        self.assertTrue(self.handle({}, missing, 1000.0, push))
        self.assertFalse(self.handle({}, missing, 1001.0, push))
        self.assertFalse(self.handle({}, watcher.CommandOutput("agent-problems", 0, "", ""), 1002.0, push))
        self.assertTrue(self.handle({}, missing, 1003.0, push))
        self.assertEqual(2, push.call_count)

    def test_unrelated_task_isolation(self) -> None:
        reason_a = "approval A"
        reason_b = "approval B"
        (self.root / "TODO.md").write_text("current:\na.md agent:2\nb.md agent:3\n", encoding="utf-8")
        (self.root / "a.md").write_text(task_frontmatter("blocked", "agent:2", "mgr:1", reason_a), encoding="utf-8")
        (self.root / "b.md").write_text(task_frontmatter("blocked", "agent:3", "mgr:1", reason_b), encoding="utf-8")
        line_a = problem_line("a.md", "agent:2", "mgr:1", reason_a)
        line_b = problem_line("b.md", "agent:3", "mgr:1", reason_b)
        push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, problem_result(line_a, line_b), 1000.0, push))

        changed_reason = "replacement approval A"
        (self.root / "a.md").write_text(task_frontmatter("blocked", "agent:2", "mgr:1", changed_reason), encoding="utf-8")
        changed_a = problem_line("a.md", "agent:2", "mgr:1", changed_reason)
        self.assertTrue(self.handle({}, problem_result(changed_a, line_b), 1001.0, push))
        message = push.call_args_list[1].args[1]
        self.assertIn("a.md", message)
        self.assertNotIn("b.md", message)

    def test_changed_authority_realerts_without_revoking_active_claim(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        line = problem_line("worker.md", "agent:2", "mgr:1", reason)
        result = problem_result(line)
        first_push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, result, 1000.0, first_push))

        problem_id = watcher.problem_claim_id("mgr:1", (line,))
        claim_path = watcher.problem_claim_path(self.args)
        claim_problem(claim_path, problem_id, "mgr:1", "verify the replacement blocker", 1000.5)
        self.write_task(reason="replacement approval")
        changed_push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, result, 1001.0, changed_push))

        alert = changed_push.call_args.args[1]
        self.assertIn("fail-closed state verification caused this re-alert", alert)
        self.assertIn("verify the replacement blocker", alert)
        self.assertNotIn("Claim responsibility for 10 minutes", alert)
        self.assertIn(problem_id, read_claims(claim_path))

    def test_v2_replacement_realerts_and_async_guard_rejects_old_identity(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason, task_id=TASK_ID_A)
        line = problem_line("worker.md", "agent:2", "mgr:1", reason)
        result = problem_result(line)
        captured: list[tuple[watcher.DeliverySuccessEvent, watcher.AgentProblemGuard]] = []

        def capture(
            _args: watcher.Args,
            _text: str,
            _target: str,
            event: watcher.DeliverySuccessEvent,
            *,
            problem_guard: watcher.AgentProblemGuard,
            **_kwargs: object,
        ) -> int:
            captured.append((event, problem_guard))
            return watcher.ASYNC_DELIVERY_STARTED

        seen: dict[str, float] = {}
        self.assertTrue(self.handle(seen, result, 1000.0, MagicMock(side_effect=capture)))
        self.assertFalse(self.handle({}, result, 1000.5, MagicMock(side_effect=capture)))
        self.assertEqual(1, len(captured))
        self.write_task(reason=reason, task_id=TASK_ID_B)

        command_result = subprocess.CompletedProcess(list(captured[0][1].command), 3, result.stdout, "")
        pasted: list[str] = []

        def guarded_send(_target: str, _message: str, _options: object, *, before_paste: object) -> None:
            assert callable(before_paste)
            before_paste()
            pasted.append("pasted")

        with patch.object(watcher.subprocess, "run", return_value=command_result), patch.object(
            watcher, "verified_send_to_codex", side_effect=guarded_send
        ), self.assertRaises(watcher.PrePasteRejected):
            watcher.run_verified_send("mgr:1", "alert", watcher.CodexSendOptions(2, 0.15, False), problem_guard=captured[0][1])
        self.assertEqual([], pasted)

        push = MagicMock(return_value=0)
        self.assertTrue(self.handle(seen, result, 1001.0, push))
        ledger = watcher.read_problem_episode_ledger(watcher.problem_episode_ledger_path(self.args))
        self.assertEqual(TASK_ID_B, next(iter(ledger.values())).record.task_id)

    def assert_async_guard_cannot_paste(
        self,
        guard: watcher.AgentProblemGuard,
        result: watcher.CommandOutput,
    ) -> None:
        command_result = subprocess.CompletedProcess(list(guard.command), 3, result.stdout, "")
        pasted: list[str] = []

        def guarded_send(_target: str, _message: str, _options: object, *, before_paste: object) -> None:
            assert callable(before_paste)
            before_paste()
            pasted.append("pasted")

        with patch.object(watcher.subprocess, "run", return_value=command_result), patch.object(
            watcher, "verified_send_to_codex", side_effect=guarded_send
        ), patch.object(watcher, "inspect_codex", side_effect=AssertionError("stale lease inspected target")), self.assertRaises(
            watcher.PrePasteRejected
        ):
            watcher.run_verified_send(
                "mgr:1",
                "alert",
                watcher.CodexSendOptions(2, 0.15, False),
                problem_guard=guard,
            )
        self.assertEqual([], pasted)

    def test_async_guard_rejects_clean_then_same_episode_recurrence(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        result = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))
        guards: list[watcher.AgentProblemGuard] = []

        def capture(
            _args: watcher.Args,
            _text: str,
            _target: str,
            _event: watcher.DeliverySuccessEvent,
            *,
            problem_guard: watcher.AgentProblemGuard,
            **_kwargs: object,
        ) -> int:
            guards.append(problem_guard)
            return watcher.ASYNC_DELIVERY_STARTED

        push = MagicMock(side_effect=capture)
        self.assertTrue(self.handle({}, result, 1000.0, push))
        self.assertFalse(self.handle({}, watcher.CommandOutput("agent-problems", 0, "", ""), 1001.0, push))
        self.assertTrue(self.handle({}, result, 1002.0, push))
        self.assertEqual(2, len(guards))
        self.assertNotEqual(guards[0].episode_reservations, guards[1].episode_reservations)
        self.assert_async_guard_cannot_paste(guards[0], result)

    def test_async_guard_rejects_a_to_b_to_a_reservation_replacement(self) -> None:
        first_reason = "first approval"
        second_reason = "second approval"
        self.write_task(reason=first_reason)
        first = problem_result(problem_line("worker.md", "agent:2", "mgr:1", first_reason))
        second = problem_result(problem_line("worker.md", "agent:2", "mgr:1", second_reason))
        guards: list[watcher.AgentProblemGuard] = []

        def capture(
            _args: watcher.Args,
            _text: str,
            _target: str,
            _event: watcher.DeliverySuccessEvent,
            *,
            problem_guard: watcher.AgentProblemGuard,
            **_kwargs: object,
        ) -> int:
            guards.append(problem_guard)
            return watcher.ASYNC_DELIVERY_STARTED

        push = MagicMock(side_effect=capture)
        self.assertTrue(self.handle({}, first, 1000.0, push))
        self.write_task(reason=second_reason)
        self.assertTrue(self.handle({}, second, 1001.0, push))
        self.write_task(reason=first_reason)
        self.assertTrue(self.handle({}, first, 1002.0, push))
        self.assertEqual(3, len(guards))
        self.assertNotEqual(guards[0].episode_reservations, guards[2].episode_reservations)
        self.assert_async_guard_cannot_paste(guards[0], first)

    def test_async_guard_rejects_target_reuse_owner_and_alias_races(self) -> None:
        reason = "waiting on approval"
        line = problem_line("worker.md", "agent:2", "mgr:1", reason)
        result = problem_result(line)

        def assert_rejected_after(mutate: object) -> None:
            self.write_task(reason=reason, task_id=TASK_ID_A)
            expectation = watcher.problem_episode_expectation(self.root, line)
            assert expectation is not None
            guard = watcher.AgentProblemGuard(
                ("status",),
                (line,),
                root=self.root,
                ready_target="mgr:1",
                episode_expectations=(expectation,),
            )
            assert callable(mutate)
            mutate()
            command_result = subprocess.CompletedProcess(["status"], 3, result.stdout, "")
            pasted: list[str] = []

            def guarded_send(_target: str, _message: str, _options: object, *, before_paste: object) -> None:
                assert callable(before_paste)
                before_paste()
                pasted.append("pasted")

            with patch.object(watcher.subprocess, "run", return_value=command_result), patch.object(
                watcher, "verified_send_to_codex", side_effect=guarded_send
            ), self.assertRaises(watcher.PrePasteRejected):
                watcher.run_verified_send("mgr:1", "alert", watcher.CodexSendOptions(2, 0.15, False), problem_guard=guard)
            self.assertEqual([], pasted)

        with self.subTest("target reuse"):
            def reuse_target() -> None:
                self.write_task(target="agent:3", reason=reason, task_id=TASK_ID_A, extra_todo=(("peer.md", "agent:2"),))
                (self.root / "peer.md").write_text(
                    task_frontmatter("running", "agent:2", "mgr:1", task_id=TASK_ID_B),
                    encoding="utf-8",
                )

            assert_rejected_after(reuse_target)

        with self.subTest("owner"):
            assert_rejected_after(lambda: self.write_task(owner_target="mgr:2", reason=reason, task_id=TASK_ID_A))

        with self.subTest("canonical alias"):
            assert_rejected_after(lambda: self.write_task(reason=reason, task_id=TASK_ID_A, todo_ref="./worker.md"))

    def test_late_async_completion_cannot_hide_newer_or_clean_state(self) -> None:
        first_reason = "first approval"
        second_reason = "second approval"
        self.write_task(reason=first_reason)
        events: list[watcher.DeliverySuccessEvent] = []

        def capture(
            _args: watcher.Args,
            _text: str,
            _target: str,
            event: watcher.DeliverySuccessEvent,
            **_kwargs: object,
        ) -> int:
            events.append(event)
            return watcher.ASYNC_DELIVERY_STARTED

        push = MagicMock(side_effect=capture)
        first = problem_result(problem_line("worker.md", "agent:2", "mgr:1", first_reason))
        self.assertTrue(self.handle({}, first, 1000.0, push))
        self.write_task(reason=second_reason)
        second = problem_result(problem_line("worker.md", "agent:2", "mgr:1", second_reason))
        self.assertTrue(self.handle({}, second, 1001.0, push))
        self.assertTrue(watcher.commit_problem_episode_reservations(self.args, events[1].problem_episode_commits))
        self.assertFalse(watcher.commit_problem_episode_reservations(self.args, events[0].problem_episode_commits))

        third_reason = "third approval"
        self.write_task(reason=third_reason)
        third = problem_result(problem_line("worker.md", "agent:2", "mgr:1", third_reason))
        self.assertTrue(self.handle({}, third, 1002.0, push))
        healthy = watcher.CommandOutput("agent-problems", 0, "", "")
        self.assertFalse(self.handle({}, healthy, 1003.0, push))
        self.assertFalse(watcher.commit_problem_episode_reservations(self.args, events[2].problem_episode_commits))
        self.assertEqual({}, watcher.read_problem_episode_ledger(watcher.problem_episode_ledger_path(self.args)))

    def test_reservation_is_atomic_and_stale_pending_realerts_fail_closed(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        line = problem_line("worker.md", "agent:2", "mgr:1", reason)
        expectation = watcher.problem_episode_expectation(self.root, line)
        assert expectation is not None and expectation.record is not None
        first = watcher.reserve_problem_episodes(self.args, (expectation.record,))
        second = watcher.reserve_problem_episodes(self.args, (expectation.record,))
        self.assertEqual(1, len(first))
        self.assertEqual((), second)
        self.assertTrue(watcher.rollback_problem_episode_reservations(self.args, first))
        with watcher.locked_problem_episode_ledger(self.args) as ledger:
            ledger[expectation.record.key] = watcher.ProblemEpisodeLedgerEntry(
                expectation.record,
                "pending",
                "orphaned-reservation",
                1.0,
            )

        push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, problem_result(line), 1000.0, push))
        push.assert_called_once()

    def test_rejected_delivery_does_not_suppress_the_episode(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        result = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))
        rejected = MagicMock(return_value=1)
        self.assertFalse(self.handle({}, result, 1000.0, rejected))
        self.assertEqual({}, watcher.read_problem_episode_ledger(watcher.problem_episode_ledger_path(self.args)))

        accepted = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, result, 1001.0, accepted))
        accepted.assert_called_once()

    def test_corrupt_ledger_realerts_and_is_replaced_atomically(self) -> None:
        reason = "waiting on approval"
        self.write_task(reason=reason)
        ledger_path = watcher.problem_episode_ledger_path(self.args)
        ledger_path.parent.mkdir(parents=True)
        ledger_path.write_text("not a problem episode ledger\n", encoding="utf-8")
        result = problem_result(problem_line("worker.md", "agent:2", "mgr:1", reason))

        push = MagicMock(return_value=0)
        self.assertTrue(self.handle({}, result, 1000.0, push))
        push.assert_called_once()
        self.assertEqual(0o600, stat.S_IMODE(ledger_path.stat().st_mode))
        self.assertFalse(any(ledger_path.parent.glob(f".{ledger_path.name}.*.tmp")))

    def test_status_helper_and_watcher_never_touch_human_owned_targets(self) -> None:
        (self.root / "TODO.md").write_text(
            "current:\nh-runat.md hcfg:2\nh-owner.md agent:3\nmalformed-owner.md agent:4\nmalformed-runat.md agent:5\n",
            encoding="utf-8",
        )
        (self.root / "h-runat.md").write_text(task_frontmatter("running", "hcfg:2", "mgr:1"), encoding="utf-8")
        (self.root / "h-owner.md").write_text(task_frontmatter("running", "agent:3", "hmgr:1"), encoding="utf-8")
        (self.root / "malformed-owner.md").write_text(
            "---\nversion: v1.0.0\nstatus: invalid\nrunat: agent:4\nmanagerat: hmgr:2\n---\n",
            encoding="utf-8",
        )
        (self.root / "malformed-runat.md").write_text(
            "---\nversion: v1.0.0\nstatus: invalid\nrunat: hcfg:5\nmanagerat: mgr:1\n---\n",
            encoding="utf-8",
        )
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        tmux_log = self.root / "tmux.log"
        fake_tmux = fake_bin / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$TMUX_TEST_LOG\"\n"
            "if [ \"$1\" = list-panes ]; then exit 0; fi\nexit 91\n",
            encoding="utf-8",
        )
        fake_tmux.chmod(0o700)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["TMUX_TEST_LOG"] = str(tmux_log)
        status_script = Path(watcher.__file__).with_name("omo_agent_status.py")
        checked = subprocess.run(
            [
                sys.executable,
                str(status_script),
                "--root",
                str(self.root),
                "--registry",
                str(self.root / "registry.json"),
                "--problems-only",
                "--no-auto-unstick",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, checked.returncode, checked.stderr)
        tmux_commands = tmux_log.read_text(encoding="utf-8") if tmux_log.exists() else ""
        self.assertNotIn("hcfg:2", tmux_commands)
        self.assertNotIn("agent:3", tmux_commands)
        self.assertNotIn("agent:4", tmux_commands)
        self.assertNotIn("agent:5", tmux_commands)
        self.assertNotIn("capture-pane", tmux_commands)
        self.assertNotIn("send-keys", tmux_commands)

        h_lines = problem_result(
            problem_line("h-runat.md", "hcfg:2", "mgr:1", "", problem_class="missing", task_status="running", idle_state="missing"),
            problem_line("h-owner.md", "agent:3", "hmgr:1", "", problem_class="missing", task_status="running", idle_state="missing"),
            problem_line("h-owner.md", "agent:3", "human-owner", "", problem_class="missing", task_status="running", idle_state="missing"),
        )
        with patch.object(watcher, "inspect_codex", side_effect=AssertionError("human target inspected")), patch.object(
            watcher, "push_manager_text_to_target", side_effect=AssertionError("human target delivered")
        ), patch.object(watcher, "email_human_manager_problem", side_effect=AssertionError("human contacted")):
            self.assertFalse(watcher.handle_agent_problem_result(self.args, {}, h_lines, 1000.0, {}))

        with patch.object(watcher, "require_sendable_codex_target") as inspect_target, patch.object(watcher, "submit_send") as submit:
            with self.assertRaises(watcher.PrePasteRejected):
                watcher.send_to_codex("hcfg:2", "forbidden")
        inspect_target.assert_not_called()
        submit.assert_not_called()
        with patch.object(watcher, "require_sendable_codex_target") as inspect_target, patch.object(watcher, "submit_send") as submit:
            with self.assertRaises(watcher.PrePasteRejected):
                watcher.send_to_codex("human-session", "forbidden")
        inspect_target.assert_not_called()
        submit.assert_not_called()
        with patch.object(watcher, "submit_delivery_send") as deliver:
            rejected = watcher.try_send_delivery_text("forbidden", "message", "hcfg:2", root=self.root)
        self.assertEqual(1, rejected.status)
        deliver.assert_not_called()
        with patch.object(watcher, "try_send_delivery_text") as deliver:
            self.assertEqual(1, watcher.push_manager_text_to_target(self.args, "forbidden", "hmgr:1"))
        deliver.assert_not_called()
        with patch.object(watcher, "verified_send_to_codex") as deliver, self.assertRaises(watcher.PrePasteRejected):
            watcher.run_ready_report_reminder("hcfg:2", "fingerprint")
        deliver.assert_not_called()
        guard = watcher.AgentProblemGuard((), (), root=self.root)
        with patch.object(watcher, "verified_send_capacity_resume") as deliver, self.assertRaises(watcher.PrePasteRejected):
            watcher.run_capacity_resume("hcfg:2", watcher.CodexSendOptions(1, 0.15, False), guard)
        deliver.assert_not_called()

    def test_marker_delivery_rejects_human_recipient_before_dry_run_output(self) -> None:
        marker = watcher.Marker(
            Path("worker.md"),
            1,
            "digest",
            "agent",
            "manager",
            "",
            "(pending)\nprivate alert",
            "",
            2,
            "",
        )
        dry_run_args = watcher.replace(self.args, dry_run=True)
        with patch("builtins.print") as output, patch.object(watcher, "try_send_delivery_text") as deliver:
            result = watcher.push_marker_delivery(dry_run_args, marker, "private alert", "human-owner")

        self.assertEqual(1, result.status)
        output.assert_not_called()
        deliver.assert_not_called()

    def test_problem_scan_command_is_always_read_only(self) -> None:
        command = watcher.status_command(self.args, True)
        self.assertIn("--problems-only", command)
        self.assertEqual(1, command.count("--no-auto-unstick"))
        human_manager_command = watcher.status_command(watcher.replace(self.args, manager_target="hmgr:1"), True)
        self.assertNotIn("--manager-target", human_manager_command)


if __name__ == "__main__":
    unittest.main()
