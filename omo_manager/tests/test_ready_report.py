from __future__ import annotations

import unittest
import tempfile
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_pending_watch as watcher
from omo_manager.omo_codex_status import Report
from omo_manager.omo_ready_report import command_invokes_report_helper
from omo_manager.omo_ready_report import displayed_shell_commands
from omo_manager.omo_ready_report import latest_visible_turn
from omo_manager.omo_ready_report import turn_invoked_report_helper
from omo_manager.omo_ready_report import VisibleTurn


class ReadyReportParserTest(unittest.TestCase):
    def test_latest_turn_starts_at_last_prompt_and_ends_at_activity_footer(self) -> None:
        lines = [
            "› older request mentioning omo_report.sh",
            "• Ran omo_report.sh --status done --message-file /tmp/old",
            "─ Worked for 2s ─────────",
            "› finish the current implementation",
            "• Ran pytest -q",
            "  └ 12 passed",
            "• Tests pass; implementation is complete.",
            "─ Worked for 8s ─────────",
            "› Use /skills to list available skills",
            "  gpt-5.6 medium",
        ]

        turn = latest_visible_turn(lines)

        self.assertIsNotNone(turn)
        assert turn is not None
        self.assertEqual("› finish the current implementation", turn.lines[0])
        self.assertEqual("─ Worked for 8s ─────────", turn.lines[-1])
        self.assertFalse(turn_invoked_report_helper(turn))
        repeated = latest_visible_turn(list(lines))
        assert repeated is not None
        self.assertEqual(turn.fingerprint, repeated.fingerprint)

    def test_extracts_wrapped_ran_command_without_tool_output(self) -> None:
        turn = latest_visible_turn(
            [
                "› report",
                "• Ran REPORT_FILE=$(omo_report.sh --alloc-message-file) && printf test",
                "  │ > /tmp/body && omo_report.sh --status done --message-file /tmp/body",
                "  └ report sent",
                "─ Worked for 1s ─────────",
            ]
        )

        assert turn is not None
        self.assertEqual(1, len(displayed_shell_commands(turn)))
        self.assertTrue(turn_invoked_report_helper(turn))

    def test_recognizes_paths_interpreters_wrappers_and_command_substitution(self) -> None:
        invocations = (
            "/home/me/.config/helper.sh/email_me.py --message-file /tmp/body",
            "python3 ../helper.sh/email_me.py --message-file /tmp/body",
            "env MODE=agent timeout 30 omo_report.sh --status done --message-file /tmp/body",
            "bash -lc 'uv run /opt/helpers/email_me.py --message-file /tmp/body'",
            "bash /opt/helpers/omo_report.sh --task-file task.md",
            "bash -o pipefail /opt/helpers/omo_report.sh --task-file task.md",
            "sh /opt/helpers/email_me.py --message-file /tmp/body",
            "uv run python3 /opt/helpers/email_me.py --message-file /tmp/body",
            "REPORT_FILE=$(omo_report.sh --alloc-message-file)",
            "find . -exec /opt/email_me.py {} ;",
            "nice -n 5 omo_report.sh --status done --message-file /tmp/body",
            "sudo -u agent omo_report.sh --status done --message-file /tmp/body",
            "timeout -k 1 30 omo_report.sh --status done --message-file /tmp/body",
            "exec -a worker omo_report.sh --status done --message-file /tmp/body",
        )
        for command in invocations:
            with self.subTest(command=command):
                self.assertTrue(command_invokes_report_helper(command))

    def test_does_not_treat_mentions_or_quoted_instructions_as_invocations(self) -> None:
        mentions = (
            'rg -n "email_me.py|omo_report.sh" .',
            "printf '%s\\n' 'run omo_report.sh when done'",
            "echo email_me.py",
            "sed -n '/omo_report.sh/p' AGENTS.md",
        )
        for command in mentions:
            with self.subTest(command=command):
                self.assertFalse(command_invokes_report_helper(command))

        turn = latest_visible_turn(
            [
                "› You must run email_me.py or omo_report.sh before stopping.",
                "• I completed the implementation.",
                "─ Worked for 3s ─────────",
            ]
        )
        assert turn is not None
        self.assertFalse(turn_invoked_report_helper(turn))


class ImmediateExecutor:
    def submit(self, function: Callable[..., None], *args: object) -> Future[None]:
        future: Future[None] = Future()
        try:
            function(*args)
        except BaseException as exc:
            future.set_exception(exc)
        else:
            future.set_result(None)
        return future


class DeferredExecutor:
    def __init__(self) -> None:
        self.futures: list[Future[None]] = []

    def submit(self, _function: Callable[..., None], *_args: object) -> Future[None]:
        future: Future[None] = Future()
        self.futures.append(future)
        return future


def task_text(*, runat: str = "agents:2", managerat: str = "agents:1", is_manager: bool = False) -> str:
    return (
        "---\n"
        "version: v1.0.0\n"
        "status: running\n"
        f"runat: {runat}\n"
        "tool: codex\n"
        f"managerat: {managerat}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        "pending_task_items: []\n"
        "---\n"
    )


class ReadyReportDeliveryTest(unittest.TestCase):
    def make_args(self, root: Path) -> watcher.Args:
        return watcher.Args(root, "", root / "watch-state.tsv", 1, 1, 1, root / "status.py", False, False, manager_target="agents:1")

    def ready_output(self, detail: str = "complete") -> str:
        return f"agent-problems: ready=1\nready: task=worker.md evidence=target=agents:2 task_status=running output={detail} owner_target=agents:1"

    def pane(self, activity: str = "• Ran pytest -q") -> list[str]:
        return [
            "› implement and report",
            activity,
            "  └ passed",
            "• Implementation complete.",
            "─ Worked for 4s ─────────",
            "› Use /skills to list available skills",
            "  gpt-5.6 medium",
        ]

    def test_fast_scan_delivers_once_to_agent_and_never_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(), encoding="utf-8")
            args = self.make_args(root)
            seen = {f"{watcher.capacity_state_prefix(args, 'old:9')}attempt:1": 90.0}
            deliveries: list[tuple[str, str]] = []

            def verified(target: str, message: str, _options: object, *, before_paste: Callable[[], None]) -> None:
                before_paste()
                deliveries.append((target, message))

            with (
                patch.object(watcher, "codex_tail", return_value=self.pane()),
                patch.object(watcher, "inspect_codex", return_value=Report("ready", ["Implementation complete."])),
                patch.object(watcher, "verified_send_to_codex", side_effect=verified),
                patch.object(watcher, "send_executor", return_value=ImmediateExecutor()),
                patch.object(watcher, "push_agent_pending_item_reminders", return_value=False),
                patch.object(watcher, "push_manager_direct_report_reminders", return_value=False),
                patch.object(watcher, "push_manager_text_to_target") as manager_delivery,
            ):
                result = watcher.CommandOutput("agent-problems", 3, self.ready_output(), "")
                self.assertTrue(watcher.handle_agent_problem_result(args, seen, result, 100.0))
                self.assertFalse(any(key.startswith(watcher.capacity_state_prefix(args, "old:9")) for key in seen))
                _ = watcher.drain_delivery_successes(args, seen, 101.0)
                seen.clear()  # Simulate a watcher restart; the durable fingerprint still deduplicates.
                self.assertFalse(watcher.handle_agent_problem_result(args, seen, result, 102.0))

            self.assertEqual([("agents:2", watcher.AGENT_READY_REPORT_REMINDER)], deliveries)
            manager_delivery.assert_not_called()
            self.assertEqual(1, len(watcher.read_ready_report_ledger(args)))

    def test_new_turn_fingerprint_is_reconsidered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(), encoding="utf-8")
            args = self.make_args(root)
            seen: dict[str, float] = {}
            first = self.pane()
            second = self.pane("• Ran ruff check omo_manager")
            submitted: list[VisibleTurn] = []

            def submit(
                _args: watcher.Args,
                _seen: dict[str, float],
                _target: str,
                turn: VisibleTurn,
                _now_wall_s: float,
                _active_target_keys: set[str],
            ) -> bool:
                submitted.append(turn)
                return True

            with patch.object(watcher, "codex_tail", side_effect=[first, second]), patch.object(watcher, "submit_ready_report_reminder", side_effect=submit):
                filtered, changed = watcher.handle_ready_report_reminders(args, seen, self.ready_output(), 100.0)
                self.assertEqual("", filtered)
                self.assertTrue(changed)
                filtered, changed = watcher.handle_ready_report_reminders(args, seen, self.ready_output("new"), 101.0)
                self.assertEqual("", filtered)
                self.assertTrue(changed)

            self.assertEqual(2, len(submitted))
            self.assertNotEqual(submitted[0].fingerprint, submitted[1].fingerprint)

    def test_helper_invocation_is_not_reminded_and_ready_submanager_is_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(), encoding="utf-8")
            args = self.make_args(root)
            invoked = self.pane("• Ran /home/me/.config/omo_manager/omo_report.sh --status done --message-file /tmp/body")
            with patch.object(watcher, "codex_tail", return_value=invoked), patch.object(watcher, "submit_ready_report_reminder") as submit:
                filtered, changed = watcher.handle_ready_report_reminders(args, {}, self.ready_output(), 100.0)
            self.assertEqual(self.ready_output(), filtered)
            self.assertFalse(changed)
            submit.assert_not_called()

            _ = (root / "worker.md").write_text(task_text(is_manager=True), encoding="utf-8")
            with patch.object(watcher, "codex_tail", return_value=self.pane()), patch.object(watcher, "submit_ready_report_reminder", return_value=True) as submit:
                filtered, changed = watcher.handle_ready_report_reminders(args, {}, self.ready_output(), 100.0)
            self.assertEqual("", filtered)
            self.assertTrue(changed)
            submit.assert_called_once()

    def test_synthetic_main_manager_self_row_is_not_a_tracked_task_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(is_manager=True), encoding="utf-8")
            args = self.make_args(root)
            output = "agent-problems: ready=1\nready: task=manager evidence=target=agents:1 role=manager output=idle"
            with patch.object(watcher, "codex_tail", side_effect=AssertionError("synthetic self row must not be inspected")):
                filtered, changed = watcher.handle_ready_report_reminders(args, {}, output, 100.0)
            self.assertEqual(output, filtered)
            self.assertFalse(changed)

    def test_stale_registry_ready_target_is_not_reminded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(), encoding="utf-8")
            args = self.make_args(root)
            output = "agent-problems: ready=1\nready: task=worker.md evidence=target=stale:9 role=registry_unmanaged output=idle"

            with patch.object(watcher, "codex_tail", side_effect=AssertionError("stale registry pane must not be inspected")):
                filtered, changed = watcher.handle_ready_report_reminders(args, {}, output, 100.0)

            self.assertEqual(output, filtered)
            self.assertFalse(changed)

    def test_durable_ledger_replaces_latest_fingerprint_per_canonical_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root)
            target_key = watcher.ready_report_target_key(args, "agents:2.0")
            for index in range(100):
                watcher.record_ready_report_key(args, target_key, f"fingerprint-{index}")

            ledger = watcher.read_ready_report_ledger(args)
            self.assertEqual({target_key: "fingerprint-99"}, ledger)
            self.assertEqual(1, len(watcher.ready_report_ledger_path(args).read_text(encoding="utf-8").splitlines()))

    def test_reservation_prevents_concurrent_watchers_and_restart_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root)
            turn = latest_visible_turn(self.pane())
            assert turn is not None
            executor = DeferredExecutor()

            def submit() -> bool:
                return watcher.submit_ready_report_reminder(args, {}, "agents:2", turn, 100.0)

            with patch.object(watcher, "send_executor", return_value=executor):
                with ThreadPoolExecutor(max_workers=2) as callers:
                    results = list(callers.map(lambda _index: submit(), range(2)))
                restarted = watcher.submit_ready_report_reminder(args, {}, "agents:2.0", turn, 101.0)

            self.assertEqual([False, True], sorted(results))
            self.assertFalse(restarted)
            self.assertEqual(1, len(executor.futures))
            executor.futures[0].set_result(None)
            watcher.drain_send_results()

    def test_definite_rejection_rolls_back_only_matching_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root)
            target_key = watcher.ready_report_target_key(args, "agents:2")
            old_key = "old-turn"
            new_key = "new-turn"
            seen = {old_key: 100.0}
            watcher.record_ready_report_key(args, target_key, old_key)
            watcher.record_ready_report_key(args, target_key, new_key)
            rejected: Future[None] = Future()
            rejected.set_exception(watcher.PrePasteRejected("changed before paste"))

            watcher.log_ready_report_result(rejected, args, target_key, old_key)
            _ = watcher.drain_delivery_successes(args, seen, 101.0)

            self.assertEqual(new_key, watcher.read_ready_report_ledger(args)[target_key])
            self.assertNotIn(old_key, seen)
            self.assertTrue(watcher.rollback_ready_report_key(args, target_key, new_key))
            self.assertNotIn(target_key, watcher.read_ready_report_ledger(args))

    def test_executor_rejection_releases_reservation_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root)
            turn = latest_visible_turn(self.pane())
            assert turn is not None

            with patch.object(watcher, "send_executor", side_effect=RuntimeError("executor stopped")):
                self.assertFalse(watcher.submit_ready_report_reminder(args, {}, "agents:2", turn, 100.0))

            self.assertEqual({}, watcher.read_ready_report_ledger(args))

    def test_unknown_async_outcome_retains_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = self.make_args(root)
            target_key = watcher.ready_report_target_key(args, "agents:2")
            turn_key = "reserved-turn"
            watcher.record_ready_report_key(args, target_key, turn_key)
            unknown: Future[None] = Future()
            unknown.set_exception(TimeoutError("submit verification timed out"))

            watcher.log_ready_report_result(unknown, args, target_key, turn_key)

            self.assertEqual(turn_key, watcher.read_ready_report_ledger(args)[target_key])

    def test_pruning_keeps_only_active_targets_for_current_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(), encoding="utf-8")
            args = self.make_args(root)
            active_key = watcher.ready_report_target_key(args, "agents:2")
            stale_key = watcher.ready_report_target_key(args, "old:9")
            other_args = self.make_args(root / "other-root")
            other_key = watcher.ready_report_target_key(other_args, "other:3")
            watcher.record_ready_report_key(args, active_key, "active-turn")
            watcher.record_ready_report_key(args, stale_key, "stale-turn")
            watcher.record_ready_report_key(args, other_key, "other-root-turn")

            active = watcher.active_ready_report_target_keys(args)
            self.assertTrue(watcher.prune_ready_report_ledger(args, active))

            self.assertEqual(
                {active_key: "active-turn", other_key: "other-root-turn"},
                watcher.read_ready_report_ledger(args),
            )

    def test_pending_item_reminder_suppresses_competing_ready_report_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nworker.md agents 2\n", encoding="utf-8")
            _ = (root / "worker.md").write_text(task_text(), encoding="utf-8")
            args = self.make_args(root)

            with patch.object(watcher, "submit_ready_report_reminder") as submit:
                filtered, changed = watcher.handle_ready_report_reminders(args, {}, self.ready_output(), 100.0, {"agents:2"})

            self.assertEqual("", filtered)
            self.assertFalse(changed)
            submit.assert_not_called()


if __name__ == "__main__":
    _ = unittest.main()
