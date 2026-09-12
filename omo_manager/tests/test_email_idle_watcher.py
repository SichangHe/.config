from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager import email_idle_watcher as watcher
from omo_manager import omo_pending_watch as pending_watcher
from omo_manager.omo_agent_status import parse_task_metadata


def task_text(
    *,
    runat: str,
    managerat: str,
    is_manager: bool = False,
    pending_items: tuple[str, ...] = (),
    status: str = "running",
) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
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
    def test_accepts_exact_case_insensitive_command_prefix_without_please_or_period(self) -> None:
        cases = {
            "REPLACE this Agent": watcher.AgentLifecycleAction.REPLACE,
            "terminate this agent": watcher.AgentLifecycleAction.TERMINATE,
            "Replace this agent.": watcher.AgentLifecycleAction.REPLACE,
            "Terminate this agent -- because it is stuck\nSteven": watcher.AgentLifecycleAction.TERMINATE,
            "Replace this agent\nExplanation starts on the next line without a blank line.": watcher.AgentLifecycleAction.REPLACE,
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                command = watcher.agent_lifecycle_command(body)
                self.assertIsNotNone(command)
                assert command is not None
                self.assertIs(expected, command.action)

    def test_replacement_preserves_everything_after_the_matched_instruction_as_context(self) -> None:
        command = watcher.agent_lifecycle_command("Replace this agent. Previous agent ignored the queue.\n-- Human")
        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(". Previous agent ignored the queue.\n-- Human", command.replacement_context)

        termination = watcher.agent_lifecycle_command("Terminate this agent because the work is obsolete")
        self.assertIsNotNone(termination)
        assert termination is not None
        self.assertEqual("", termination.replacement_context)

        crlf = watcher.agent_lifecycle_command("Replace this agent.\r\nExact Windows-style reason\r\n-- Human")
        self.assertIsNotNone(crlf)
        assert crlf is not None
        self.assertEqual(".\r\nExact Windows-style reason\r\n-- Human", crlf.replacement_context)

    def test_lifecycle_guidance_delivers_context_and_ordered_queue_as_text(self) -> None:
        binding = watcher.AgentLifecycleBinding(
            Path("worker.md"),
            "worker:2",
            "mgr:1",
            Path("manager.md"),
            "running",
            False,
            "0" * 64,
            ("first task", "second task"),
        )
        replacement = watcher.lifecycle_action_guidance(
            watcher.AgentLifecycleCommand(
                watcher.AgentLifecycleAction.REPLACE,
                replacement_context="\nThe previous agent ignored the requested tests.",
            ),
            binding,
            main_manager=False,
        )
        termination = watcher.lifecycle_action_guidance(
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
            binding,
            main_manager=False,
        )

        self.assertIn(
            'decision: after replacement, deliver this exact JSON-decoded text to the replacement agent: "\\nThe previous agent ignored the requested tests."',
            replacement,
        )
        self.assertIn(
            'decision: ordered queued task message text (do not record in manager pending_task_items): ["first task","second task"]',
            termination,
        )

    def test_lifecycle_delivery_does_not_inline_custody_payload(self) -> None:
        context = "reason-" + ("x" * pending_watcher.LIFECYCLE_DELIVERY_CHAR_LIMIT)
        items = tuple(f"task-{index}-" + ("y" * 500) for index in range(12))
        binding = watcher.AgentLifecycleBinding(
            Path("worker.md"),
            "worker:2",
            "mgr:1",
            Path("manager.md"),
            "running",
            False,
            "0" * 64,
            items,
        )
        termination_marker = pending_watcher.Marker(
            file=Path("worker.md"),
            line=1,
            digest="0" * 64,
            origin="human",
            source="manager_mail/42.txt",
            delegate_source="manager_mail/42.txt",
            pending_tail="",
            block_text="\n".join(
                (
                    pending_watcher.LIFECYCLE_EXPLANATION,
                    "requested_action: terminate",
                    *watcher.lifecycle_action_guidance(
                        watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE),
                        binding,
                        main_manager=False,
                    ),
                )
            ),
            file_lines=1,
            blocked_reason="",
        )
        replacement_marker = pending_watcher.Marker(
            file=Path("worker.md"),
            line=1,
            digest="0" * 64,
            origin="human",
            source="manager_mail/42.txt",
            delegate_source="manager_mail/42.txt",
            pending_tail="",
            block_text="\n".join(
                (
                    pending_watcher.LIFECYCLE_EXPLANATION,
                    "requested_action: replace",
                    *watcher.lifecycle_action_guidance(
                        watcher.AgentLifecycleCommand(
                            watcher.AgentLifecycleAction.REPLACE,
                            replacement_context=context,
                        ),
                        binding,
                        main_manager=False,
                    ),
                )
            ),
            file_lines=1,
            blocked_reason="",
        )

        termination_delivery = pending_watcher.lifecycle_marker_delivery_text(termination_marker, ())
        replacement_delivery = pending_watcher.lifecycle_marker_delivery_text(replacement_marker, ())
        self.assertNotIn(json.dumps(list(items), ensure_ascii=False, separators=(",", ":")), termination_delivery)
        self.assertNotIn(json.dumps(context, ensure_ascii=False), replacement_delivery)
        self.assertLess(len(termination_delivery), 300)
        self.assertLess(len(replacement_delivery), 300)

    def test_rejects_please_synonyms_nonleading_and_incomplete_phrases(self) -> None:
        for body in (
            "Please replace this agent.",
            "Please terminate this agent.",
            "Could you replace this agent?",
            "swap out this agent",
            "close this agent",
            "Do not terminate this agent",
            "Thanks.\n\nTerminate this agent",
            "> terminate this agent",
            "replace this agency",
            "Replace this agent's instructions",
            "Terminate this agent’s session",
            "Stop saying DeepWiki. It’s DeGenTWeb, and you should simply say dw",
        ):
            with self.subTest(body=body):
                self.assertIsNone(watcher.agent_lifecycle_command(body))


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
        self.assertIn(
            'decision: ordered queued task message text (do not record in manager pending_task_items): ["first task","second task"]',
            marker.block_text,
        )
        self.assertIn("The Human asked to terminate worker:2.", delivery)
        self.assertNotIn("addressed_task:", delivery)
        self.assertNotIn("omo_queue_transfer.py", delivery)
        self.assertNotIn("pending_task_items_ordered_json", delivery)
        self.assertIn("Terminate this agent and preserve the queue.", delivery)
        self.assertIn("custody record is stored", delivery)
        self.assertNotIn("<human_instruction>", delivery)

        consumed_worker = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        responsible_manager = parse_task_metadata(self.manager_task.read_text(encoding="utf-8"), self.root)
        assert consumed_worker is not None and responsible_manager is not None
        self.assertEqual(("first task", "second task"), consumed_worker.pending_task_items)
        self.assertEqual((), responsible_manager.pending_task_items)
        self.assertIn("(pending)", self.worker_task.read_text(encoding="utf-8"))
        event = pending_watcher.lifecycle_delivery_event(self._pending_args(), marker, "delivery-key", 1.0)
        self.assertEqual(self.root, event.clear_root)
        self.assertEqual(marker, event.clear_marker)

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
        consumed = parse_task_metadata(self.worker_task.read_text(encoding="utf-8"), self.root)
        assert consumed is not None
        self.assertEqual(("preserve me",), consumed.pending_task_items)
        self.assertIn("delivery_rule:", self.worker_task.read_text(encoding="utf-8"))

    def test_replacement_routes_trailing_email_text_for_delivery_to_successor(self) -> None:
        command = watcher.agent_lifecycle_command("Replace this agent\nThe previous agent ignored the requested tests.")
        assert command is not None
        self.mail.write_text(
            "Subject: Re: work\n\nReplace this agent\nThe previous agent ignored the requested tests.\n",
            encoding="utf-8",
        )
        watcher.append_agent_lifecycle_pending(self.args, self.mail, "Re: [worker:2] work", command)
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        delivery = pending_watcher.marker_delivery_text(
            marker,
            [pending_watcher.source_attachment(self.root, marker.delegate_source)],
            manager_only=True,
        )

        self.assertIn("The Human asked to replace worker:2.", delivery)
        self.assertIn("The previous agent ignored the requested tests.", delivery)

    def test_manager_review_delivery_is_bounded_and_does_not_expand_referenced_records(self) -> None:
        manager_body = "manager history that must not be delivered\n" * 833
        self.main_file.write_text(manager_body, encoding="utf-8")
        source1717 = self.root / "manager_mail" / "1717.txt"
        source1717.write_text("unrelated B12 history\n" * 200, encoding="utf-8")
        self.worker_task.write_text(
            task_text(
                runat="dw:0",
                managerat="main:0",
                is_manager=True,
                status="long_running",
                pending_items=("prior item from manager_mail/1717.txt with unrelated B12 history",),
            )
            + ("addressed task history that must not be delivered\n" * 200),
            encoding="utf-8",
        )
        self.mail.write_text("Subject: Re: dw manager\n\nReview this lifecycle request.\n", encoding="utf-8")

        route, _line = watcher.append_agent_lifecycle_pending(
            self.args,
            self.mail,
            "Re: [dw:0] work",
            watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.REVIEW, "the wording requires review"),
        )
        marker = pending_watcher.find_markers(self.root, [self.worker_task])[0]
        attachments = pending_watcher.marker_attachments(self._pending_args(), marker)
        delivery = pending_watcher.marker_delivery_text(marker, attachments, manager_only=True)

        self.assertEqual("main:0", route.manager_target)
        self.assertEqual(["manager_mail/42.txt"], [attachment.source for attachment in attachments])
        self.assertLessEqual(len(delivery), pending_watcher.LIFECYCLE_DELIVERY_CHAR_LIMIT)
        self.assertIn("An exact agent lifecycle command for dw:0 needs manager handling", delivery)
        self.assertNotIn("addressed_task:", delivery)
        self.assertNotIn("responsible_manager_target:", delivery)
        self.assertNotIn("task_status:", delivery)
        self.assertIn("Review this lifecycle request.", delivery)
        self.assertNotIn("pending_task_items_ordered_json", delivery)
        self.assertNotIn("task_sha256_before_transport", delivery)
        self.assertNotIn("consume_command", delivery)
        self.assertNotIn("unrelated B12 history", delivery)
        self.assertNotIn("addressed task history", delivery)
        self.assertNotIn("manager history", delivery)

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
        self.mail.write_bytes(
            b"Subject: Re: [wl:1] DeepWiki root ownership restored\n\n"
            b"Replace this agent.\r\nPrevious main manager failed to follow the replacement request."
        )
        command = watcher.agent_lifecycle_command(
            "Replace this agent.\r\nPrevious main manager failed to follow the replacement request."
        )
        assert command is not None
        live_args = watcher.replace(self.args, manager_target="wl:1")
        route, _line = watcher.append_agent_lifecycle_pending(
            live_args,
            self.mail,
            "Re: [wl:1] DeepWiki root ownership restored",
            command,
        )
        self.assertEqual(self.main_file.resolve(), route.manager_file)
        text = self.main_file.read_text(encoding="utf-8")
        self.assertIn("requested_action: replace", text)
        self.assertIn("supported_tool: omo_manager_rotate.py", text)
        self.assertIn("supported_command: omo_manager_rotate.py --target wl:1", text)
        self.assertIn("--replacement-email-file", text)
        self.assertIn('".\\r\\nPrevious main manager failed to follow the replacement request."', text)
        self.assertIn("delivery_rule:", self.main_file.read_text(encoding="utf-8"))

    def test_live_wl1_replacement_invokes_rotation_instead_of_forwarding_to_old_agent(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        raw_message = (
            b"From: Human <human@example.test>\r\n"
            b"Subject: Re: [wl:1] DeepWiki root ownership restored\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Replace this agent.\r\nPrevious main manager failed to follow the replacement request.\r\n"
        )

        class Client:
            def uid(self, command: str, *arguments: object) -> tuple[str, list[object]]:
                if command == "fetch" and arguments == ("1751", "(BODY.PEEK[])"):
                    return "OK", [(b"RFC822", raw_message)]
                raise AssertionError((command, arguments))

        with (
            patch.object(watcher, "search_sender_uids", return_value={b"1751"}),
            patch.object(watcher, "exact_human_sender", return_value=True),
            patch.object(watcher, "execute_main_manager_replacement", return_value="audit_record: /private/rotation.json") as rotate,
            patch.object(watcher, "push_email_ref") as direct_push,
            patch.object(watcher, "mark_seen_after_human_intake", return_value=True),
            patch.object(watcher, "maybe_handle_manager_mail_thresholds", return_value=False),
        ):
            self.assertTrue(watcher.handle_unseen(Client(), live_args))  # type: ignore[arg-type]

        rotate.assert_called_once()
        replacement_mail = rotate.call_args.args[1]
        self.assertEqual("manager_mail/1751.txt", str(replacement_mail.relative_to(self.root)))
        direct_push.assert_not_called()
        self.assertNotIn("(pending)", self.main_file.read_text(encoding="utf-8"))
        self.assertIn("authenticated main-manager replacement completed", self.main_file.read_text(encoding="utf-8"))

    def test_main_replacement_runner_is_exact_and_success_receipt_prevents_replay(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        audit = self.root / "state" / "rotation.json"
        audit.write_text(
            json.dumps({"outcome": "succeeded", "target": "wl:1.0", "replacement_email_file": str(self.mail)}) + "\n",
            encoding="utf-8",
        )
        completed = watcher.subprocess.CompletedProcess([], 0, f"audit_record: {audit}\n", "")
        with patch.object(watcher.subprocess, "run", return_value=completed) as run:
            first = watcher.execute_main_manager_replacement(live_args, self.mail)
            second = watcher.execute_main_manager_replacement(live_args, self.mail)

        self.assertEqual(first, second)
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(str(Path(watcher.__file__).with_name("omo_manager_rotate.py")), command[0])
        self.assertEqual("wl:1", command[command.index("--target") + 1])
        self.assertEqual(str(self.mail), command[command.index("--replacement-email-file") + 1])

    def test_main_replacement_runner_rejects_handoff_or_failed_audit(self) -> None:
        live_args = watcher.replace(self.args, manager_target="wl:1")
        self.mail.write_bytes(b"Subject: Re: [wl:1] work\n\nReplace this agent. exact reason")
        handoff = watcher.subprocess.CompletedProcess([], 0, "coordinator_log: /private/handoff.log\n", "")
        with patch.object(watcher.subprocess, "run", return_value=handoff):
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                watcher.execute_main_manager_replacement(live_args, self.mail)

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

    def test_lifecycle_transport_replay_is_idempotent(self) -> None:
        command = watcher.AgentLifecycleCommand(watcher.AgentLifecycleAction.TERMINATE)
        _route, first_line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work", command
        )
        after_first = self.worker_task.read_bytes()

        _route, replay_line = watcher.append_agent_lifecycle_pending(
            self.args, self.mail, "Re: [worker:2] work", command
        )

        self.assertEqual(after_first, self.worker_task.read_bytes())
        self.assertGreater(first_line, 0)
        self.assertGreater(replay_line, 0)
        self.assertEqual(1, len(pending_watcher.find_markers(self.root, [self.worker_task])))

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

if __name__ == "__main__":
    unittest.main()
