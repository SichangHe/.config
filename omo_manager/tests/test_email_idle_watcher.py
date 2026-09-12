from __future__ import annotations

import tempfile
import unittest
import shlex
from pathlib import Path
from unittest.mock import patch

from omo_manager import email_idle_watcher as watcher
from omo_manager import omo_pending_watch as pending_watcher
from omo_manager import omo_record_pending
from omo_manager import omo_task_edit
from omo_manager.omo_agent_status import parse_task_metadata


def task_text(
    *,
    runat: str,
    managerat: str,
    is_manager: bool = False,
    pending_items: tuple[str, ...] = (),
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        "status: running",
        f"runat: {runat}",
        "tool: codex",
        f"managerat: {managerat}",
        f"is_manager: {str(is_manager).lower()}",
    ]
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.extend(("---", "task body", ""))
    return "\n".join(lines)


def args_for(root: Path, manager_file: Path) -> watcher.Args:
    return watcher.Args(
        root=root,
        manager_url="",
        mail_dir=root / "manager_mail",
        state_dir=root / "state",
        manager_file=manager_file,
        once=True,
        self_email="human@example.test",
        recovery_debounce_s=0,
        restart_script=Path("/bin/false"),
        manager_target="main:0",
        mail_thresholds=False,
    )


class AgentLifecycleCommandParserTests(unittest.TestCase):
    def test_accepts_lenient_case_insensitive_commands_only_in_leading_reply(self) -> None:
        cases = {
            "Could you please REPLACE this stuck Agent?": watcher.AgentLifecycleAction.REPLACE,
            "hello, I think we should shut down the responding worker now": watcher.AgentLifecycleAction.TERMINATE,
            "Kindly swap out this assistant, please.": watcher.AgentLifecycleAction.REPLACE,
            "close it please": watcher.AgentLifecycleAction.TERMINATE,
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                command = watcher.agent_lifecycle_command(body)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertIs(expected, command.action)

        self.assertIsNone(watcher.agent_lifecycle_command("Thanks.\n\n> terminate this agent"))

    def test_ambiguous_or_negated_wording_requires_manager_review(self) -> None:
        for body in (
            "Do not terminate this agent",
            "Replace or terminate this worker",
            "I was thinking about it. Replace this agent",
            "stop this agent from sending reports",
            "remove this worker's old task",
            "close this assistant's issue",
        ):
            with self.subTest(body=body):
                command = watcher.agent_lifecycle_command(body)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertIs(watcher.AgentLifecycleAction.REVIEW, command.action)


class AgentLifecycleRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="omo-email-lifecycle.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "manager_mail").mkdir()
        (self.root / "state").mkdir()
        self.main_file = self.root / "work_manager_today.md"
        self.main_file.write_text("main manager log\n", encoding="utf-8")
        self.manager_task = self.root / "submanager.md"
        self.manager_task.write_text(task_text(runat="mgr:1", managerat="main:0", is_manager=True), encoding="utf-8")
        self.worker_task = self.root / "worker.md"
        self.worker_task.write_text(
            task_text(runat="worker:2", managerat="mgr:1", pending_items=("first task", "second task")),
            encoding="utf-8",
        )
        self.mail = self.root / "manager_mail" / "42.txt"
        self.mail.write_text("Subject: Re: work\n\nTerminate this agent and preserve the queue.\n", encoding="utf-8")
        self.args = args_for(self.root, self.main_file)
        manager_custody = patch.object(watcher, "require_sendable_codex_target", return_value=None)
        manager_custody.start()
        self.addCleanup(manager_custody.stop)

    def test_termination_preserves_queue_and_routes_manager_only_transport(self) -> None:
        before = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert before is not None
        route, pending_line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
        )
        after = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert after is not None

        self.assertEqual(("first task", "second task"), after.pending_task_items)
        self.assertEqual(before.pending_task_items, after.pending_task_items)
        self.assertEqual(self.worker_task.resolve(), route.manager_file)
        self.assertEqual("mgr:1", route.manager_target)
        self.assertTrue(route.pending_watcher_delivery)
        self.assertGreater(pending_line, 1)

        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        attachments = [pending_watcher.source_attachment(self.root, marker.delegate_source)]
        delivery = pending_watcher.marker_delivery_text(marker, attachments, manager_only=True)
        self.assertEqual("mgr:1", pending_watcher.marker_for_manager_target(self._pending_args(), marker))
        self.assertTrue(pending_watcher.marker_is_for_manager(marker, attachments))
        self.assertIn("omo_queue_transfer.py", delivery)
        self.assertIn('pending_task_items_ordered_json: ["first task","second task"]', delivery)
        self.assertIn("Terminate this agent and preserve the queue.", delivery)
        self.assertIn("generated by the watcher, not Human text", delivery)
        self.assertNotIn("<human_instruction>", delivery)

        consume_line = next(line for line in marker.block_text.splitlines() if line.startswith("consume_command: "))
        consume_args = omo_record_pending.parse_args(shlex.split(consume_line.removeprefix("consume_command: "))[1:])
        with patch.object(omo_record_pending, "send_human_ack"):
            self.assertEqual(0, omo_record_pending.run(consume_args))
        consumed_worker = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        responsible_manager = parse_task_metadata(self.manager_task.read_text(encoding="utf-8"), self.root)
        assert consumed_worker is not None and responsible_manager is not None
        self.assertEqual(("first task", "second task"), consumed_worker.pending_task_items)
        self.assertIn("🧑 Handle the Human lifecycle request from `manager_mail/42.txt`.", responsible_manager.pending_task_items)
        self.assertNotIn("(pending)", self.worker_task.read_text(encoding="utf-8"))

    def test_main_managed_worker_routes_action_to_main_log(self) -> None:
        self.worker_task.write_text(
            task_text(runat="worker:2", managerat="main:0", pending_items=("preserve me",)),
            encoding="utf-8",
        )
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
        )
        text = self.worker_task.read_text(encoding="utf-8")
        self.assertEqual(self.worker_task.resolve(), route.manager_file)
        self.assertEqual("main:0", route.manager_target)
        self.assertIn("requested_action: terminate", text)
        self.assertIn("responsible_manager_task: work_manager_today.md", text)
        self.assertIn('pending_task_items_ordered_json: ["preserve me"]', text)
        self._consume_main_manager_marker(self.worker_task)
        consumed = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert consumed is not None
        self.assertEqual(("preserve me",), consumed.pending_task_items)
        self.assertNotIn("(pending)", self.worker_task.read_text(encoding="utf-8"))

    def test_main_manager_termination_is_review_only(self) -> None:
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [main] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
        )
        text = self.main_file.read_text(encoding="utf-8")
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        self.assertIn("requested_action: review", text)
        self.assertIn("main manager may be replaced but never terminated", text)
        self.assertNotIn("omo_task_status.py TASK.md done", text)

    def test_missing_main_manager_custody_refuses_fallback(self) -> None:
        self.main_file.unlink()
        with self.assertRaisesRegex(RuntimeError, "main-manager transport custody"):
            watcher.append_agent_lifecycle_pending(
                self.args,
                self.mail,
                "Re: [missing:4] work",
                watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
            )
        self.assertFalse(self.main_file.exists())

    def test_missing_main_manager_pane_refuses_transport(self) -> None:
        with (
            patch.object(watcher, "require_sendable_codex_target", side_effect=RuntimeError("target does not exist")),
            self.assertRaisesRegex(RuntimeError, "main-manager transport custody.*target does not exist"),
        ):
            watcher.append_agent_lifecycle_pending(
                self.args,
                self.mail,
                "Re: [main] work",
                watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
            )
        self.assertEqual("main manager log\n", self.main_file.read_text(encoding="utf-8"))

    def test_main_manager_replacement_uses_manager_rotation(self) -> None:
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [main] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
        )
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        text = self.main_file.read_text(encoding="utf-8")
        self.assertIn("requested_action: replace", text)
        self.assertIn("supported_tool: omo_manager_rotate.py", text)
        self._consume_main_manager_marker(self.main_file)
        self.assertNotIn("(pending)", self.main_file.read_text(encoding="utf-8"))

    def test_new_tagged_message_is_not_reply_bound(self) -> None:
        self.assertFalse(watcher.lifecycle_reply_candidate("[worker:2] work"))
        self.assertTrue(watcher.lifecycle_reply_candidate("Re: [worker:2] work"))
        self.assertTrue(watcher.lifecycle_reply_candidate("Re: worker:2 work"))
        self.assertEqual("", watcher.lifecycle_subject_target(self.args, "[worker:2] work"))
        self.assertEqual("worker:2", watcher.lifecycle_subject_target(self.args, "Re: [worker:2] work"))
        self.assertEqual("worker:2", watcher.lifecycle_subject_target(self.args, "Re: worker:2 work"))

    def test_unresolved_amh_reply_command_is_intercepted_before_amh(self) -> None:
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Subject: Re: [pb] work\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Replace this agent\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "try_route_amh_message") as amh_route,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), self.args))  # type: ignore[arg-type]

        amh_route.assert_not_called()
        text = self.main_file.read_text(encoding="utf-8")
        self.assertIn("requested_action: review", text)
        self.assertIn("no exact active task custody", text)

    def test_replay_converts_ordinary_worker_marker_before_delivery(self) -> None:
        self.worker_task.write_text(
            f"{self.worker_task.read_text(encoding='utf-8')}(pending)\n(record and delegate manager_mail/7.txt)\n",
            encoding="utf-8",
        )
        replay_mail = self.root / "manager_mail" / "7.txt"
        replay_mail.write_text("Subject: Re: work\n\nTerminate this agent\n", encoding="utf-8")
        watcher.save_processed_uids(watcher.unaccepted_pending_uids_path(self.args), {"7"})
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Subject: Re: [worker:2] work\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Terminate this agent\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("7", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"7"}),
            patch.object(watcher, "search_processed_sender_uids", return_value={b"7"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "try_route_amh_message") as amh_route,
            patch.object(watcher, "push_email_ref") as direct_push,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), self.args))  # type: ignore[arg-type]

        amh_route.assert_not_called()
        direct_push.assert_not_called()
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        attachments = [pending_watcher.source_attachment(self.root, marker.delegate_source)]
        self.assertTrue(pending_watcher.marker_is_for_manager(marker, attachments))
        self.assertEqual("mgr:1", pending_watcher.marker_for_manager_target(self._pending_args(), marker))
        self.assertIn("requested_action: terminate", marker.block_text)

    def test_ambiguous_owner_fails_closed_to_main_manager(self) -> None:
        duplicate = self.root / "worker_duplicate.md"
        duplicate.write_text(task_text(runat="worker:2", managerat="mgr:1"), encoding="utf-8")
        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [worker:2] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REPLACE),
        )
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        self.assertIn("found 2", self.main_file.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", self.worker_task.read_text(encoding="utf-8"))
        self.assertNotIn("(pending)", duplicate.read_text(encoding="utf-8"))

    def test_consumed_lifecycle_transport_is_replay_idempotent(self) -> None:
        command = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE)
        _route, first_line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work", command
        )
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        consume_line = next(line for line in marker.block_text.splitlines() if line.startswith("consume_command: "))
        consume_args = omo_record_pending.parse_args(shlex.split(consume_line.removeprefix("consume_command: "))[1:])
        with patch.object(omo_record_pending, "send_human_ack"):
            self.assertEqual(0, omo_record_pending.run(consume_args))
        after_consumption = self.worker_task.read_bytes()

        _route, replay_line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work", command
        )

        self.assertEqual(after_consumption, self.worker_task.read_bytes())
        self.assertGreater(first_line, 0)
        self.assertGreater(replay_line, 0)
        self.assertEqual([], pending_watcher.find_markers(self.root, [self.worker_task]))

    def _pending_args(self) -> pending_watcher.Args:
        return pending_watcher.Args(
            root=self.root,
            manager_url="",
            state=self.root / "seen.tsv",
            interval_s=1.0,
            full_scan_interval_s=1.0,
            idle_status_interval_s=1.0,
            status_script=Path("/bin/false"),
            once=True,
            dry_run=True,
            manager_target="main:0",
        )

    def _consume_main_manager_marker(self, task_file: Path) -> None:
        marker = pending_watcher.find_markers(self.root, [task_file])[0]
        attachments = [pending_watcher.source_attachment(self.root, marker.delegate_source)]
        delivery = pending_watcher.marker_delivery_text(marker, attachments, manager_only=True)
        self.assertTrue(pending_watcher.marker_is_for_manager(marker, attachments))
        self.assertEqual("main:0", pending_watcher.marker_for_manager_target(self._pending_args(), marker))
        self.assertIn("consume_override:", delivery)
        consume_line = next(line for line in marker.block_text.splitlines() if line.startswith("consume_command: "))
        consume_args = omo_task_edit.parse_args(shlex.split(consume_line.removeprefix("consume_command: "))[1:])
        with patch.object(omo_task_edit, "send_marker_clear_ack"):
            self.assertEqual(0, omo_task_edit.run(consume_args))


if __name__ == "__main__":
    unittest.main()
