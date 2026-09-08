from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_containment_close import (
    Args,
    AuthorityBinding,
    CloseError,
    CurrentTaskBinding,
    SentinelLive,
    authority_binding,
    close_binding,
    close_condition,
    close_contained_sentinel,
    close_record,
    closed_binding_absent,
    current_task_binding,
    guarded_sentinel_close,
    receipt_bytes,
)
from omo_manager.tests import test_manager_containment_launch as launch_tests


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ManagerContainmentCloseTests(unittest.TestCase):
    def fixture(self, base: Path, *, dry_run: bool = False) -> tuple[Args, object, CurrentTaskBinding, SentinelLive]:
        launch_case = launch_tests.ManagerContainmentLaunchTests(methodName="runTest")
        bridge, containment = launch_case.fixture(base)
        current = current_task_binding(
            Args(
                bridge,
                bridge.expected_task_sha256,
                digest((bridge.root / "TODO.md").read_bytes()),
                "blocked",
                bridge.expected_blocker,
                bridge.expected_manager_target,
                Path("manager_mail/authority.txt"),
                (1, 3),
                "a" * 64,
                Path("authority-envelope.md"),
                "b" * 64,
                bridge.containment_receipt.with_name(f"{bridge.containment_receipt.stem}-sentinel-close.receipt"),
                dry_run,
            )
        )
        live = launch_case.live_fixture(containment)
        sentinel = SentinelLive(live.pane, live.sentinel, live.protected)
        args = Args(
            bridge,
            bridge.expected_task_sha256,
            digest((bridge.root / "TODO.md").read_bytes()),
            "blocked",
            bridge.expected_blocker,
            bridge.expected_manager_target,
            Path("manager_mail/authority.txt"),
            (1, 3),
            "a" * 64,
            Path("authority-envelope.md"),
            "b" * 64,
            bridge.containment_receipt.with_name(f"{bridge.containment_receipt.stem}-sentinel-close.receipt"),
            dry_run,
        )
        return args, containment, current, sentinel

    def test_close_writes_complete_receipt_without_launching_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, containment, current, live = self.fixture(Path(tmp))
            authority = AuthorityBinding("manager_mail/authority.txt:1-3", "a" * 64, "1-3", "c" * 64, "authority-envelope.md", "b" * 64)
            with (
                patch("omo_manager.omo_manager_containment_close.containment_receipt", return_value=containment),
                patch("omo_manager.omo_manager_containment_close.current_task_binding", return_value=current),
                patch("omo_manager.omo_manager_containment_close.authority_binding", return_value=authority),
                patch("omo_manager.omo_manager_containment_close.close_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_close.sentinel_live", return_value=live),
                patch("omo_manager.omo_manager_containment_close.revalidate_sentinel", return_value=live),
                patch("omo_manager.omo_manager_containment_close.guarded_sentinel_close") as guarded,
                patch("omo_manager.omo_manager_containment_close.wait_closed"),
                patch("omo_manager.omo_manager_containment_close.process_binding_absent", return_value=True),
            ):
                result, receipt = close_contained_sentinel(args)

            self.assertEqual("closed-without-successor", result)
            self.assertEqual(args.close_receipt, receipt)
            guarded.assert_called_once_with(live)
            payload = json.loads(args.close_receipt.read_text(encoding="utf-8"))
            self.assertEqual("complete", payload["state"])
            self.assertEqual("guarded-close-confirmed", payload["completion_kind"])
            self.assertFalse(payload["binding"]["successor_launched"])
            self.assertFalse(args.bridge.ownership_receipt.exists())

    def test_dry_run_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, containment, current, live = self.fixture(Path(tmp), dry_run=True)
            authority = AuthorityBinding("manager_mail/authority.txt:1-3", "a" * 64, "1-3", "c" * 64, "authority-envelope.md", "b" * 64)
            with (
                patch("omo_manager.omo_manager_containment_close.containment_receipt", return_value=containment),
                patch("omo_manager.omo_manager_containment_close.current_task_binding", return_value=current),
                patch("omo_manager.omo_manager_containment_close.authority_binding", return_value=authority),
                patch("omo_manager.omo_manager_containment_close.close_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_close.sentinel_live", return_value=live),
                patch("omo_manager.omo_manager_containment_close.revalidate_sentinel", return_value=live),
                patch("omo_manager.omo_manager_containment_close.guarded_sentinel_close") as guarded,
            ):
                result, receipt = close_contained_sentinel(args)

            self.assertEqual(("sentinel-close-ready", None), (result, receipt))
            self.assertFalse(args.close_receipt.exists())
            guarded.assert_not_called()

    def test_prepared_receipt_recovers_only_after_target_and_process_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, containment, current, _live = self.fixture(Path(tmp))
            authority = AuthorityBinding("manager_mail/authority.txt:1-3", "a" * 64, "1-3", "c" * 64, "authority-envelope.md", "b" * 64)
            binding = close_binding(args, containment, current, authority)
            prepared = close_record(binding, "prepared-or-committed", "2026-09-08T00:00:00+00:00")
            args.close_receipt.write_bytes(receipt_bytes(prepared))
            args.close_receipt.chmod(0o600)
            with (
                patch("omo_manager.omo_manager_containment_close.containment_receipt", return_value=containment),
                patch("omo_manager.omo_manager_containment_close.current_task_binding", return_value=current),
                patch("omo_manager.omo_manager_containment_close.authority_binding", return_value=authority),
                patch("omo_manager.omo_manager_containment_close.close_locks", return_value=nullcontext()),
                patch("omo_manager.omo_manager_containment_close.closed_binding_absent", return_value=True),
                patch("omo_manager.omo_manager_containment_close.guarded_sentinel_close") as guarded,
            ):
                result, _ = close_contained_sentinel(args)

            self.assertEqual("closed-without-successor", result)
            guarded.assert_not_called()
            payload = json.loads(args.close_receipt.read_text(encoding="utf-8"))
            self.assertEqual("complete", payload["state"])
            self.assertEqual("recovered-target-absent", payload["completion_kind"])

    def test_recovery_rejects_reused_symbolic_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, containment, current, _live = self.fixture(Path(tmp))
            authority = AuthorityBinding("manager_mail/authority.txt:1-3", "a" * 64, "1-3", "c" * 64, "authority-envelope.md", "b" * 64)
            binding = close_binding(args, containment, current, authority)
            replacement = __import__("subprocess").CompletedProcess([], 0, "manager:0.0\t@999\t%999\n", "")
            with (
                patch("omo_manager.omo_manager_containment_close.subprocess.run", return_value=replacement),
                patch("omo_manager.omo_manager_containment_close.process_binding_absent", return_value=True),
            ):
                self.assertFalse(closed_binding_absent(binding))

    def test_current_task_binding_accepts_done_record_in_previous_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _containment, _current, _live = self.fixture(Path(tmp))
            task_text = args.bridge.task_file.read_text(encoding="utf-8")
            task_text = task_text.replace("status: blocked\nblocked_on: repair.md\n", "status: done\n")
            args.bridge.task_file.write_text(task_text, encoding="utf-8")
            todo_path = args.bridge.root / "TODO.md"
            todo_text = todo_path.read_text(encoding="utf-8")
            todo_text = todo_text.replace("current:\nmanager.md manager:0\n", "current:\n")
            todo_text = todo_text.replace("previous:\n", "previous:\nmanager.md manager:0\n")
            todo_path.write_text(todo_text, encoding="utf-8")
            args = replace(
                args,
                expected_current_task_sha256=digest(task_text.encode()),
                expected_current_todo_sha256=digest(todo_text.encode()),
                expected_current_status="done",
                expected_current_blocker="",
            )

            current = current_task_binding(args)

            self.assertEqual("done", current.status)
            self.assertEqual("previous", current.todo_section)

    def test_authority_requires_exact_target_in_direct_human_close_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args, _containment, _current, _live = self.fixture(Path(tmp))
            mail_dir = args.bridge.root / "manager_mail"
            mail_dir.mkdir(mode=0o700)
            source = mail_dir / "authority.txt"
            source_text = "Subject: Re: Close obsolete planner?\n\nClose manager:0.\n"
            source.write_text(source_text, encoding="utf-8")
            source.chmod(0o600)
            envelope = args.bridge.root / "authority-envelope.md"
            locator = "manager_mail/authority.txt:1-3"
            envelope_text = f'<human_instruction authoritative="true" source="{locator}">\n{source_text}</human_instruction>\n'
            envelope.write_text(envelope_text, encoding="utf-8")
            envelope.chmod(0o600)
            args = replace(
                args,
                authority_file=Path("manager_mail/authority.txt"),
                authority_sha256=digest(source_text.encode()),
                authority_envelope=Path("authority-envelope.md"),
                authority_envelope_sha256=digest(envelope_text.encode()),
            )
            self.assertEqual(locator, authority_binding(args).source)

            generic = "Subject: Re: Close obsolete planner?\n\nClose them all\n\n> Should I close manager:0?\n"
            source.write_text(generic, encoding="utf-8")
            generic_locator = "manager_mail/authority.txt:1-5"
            envelope_text = f'<human_instruction authoritative="true" source="{generic_locator}">\n{generic}</human_instruction>\n'
            envelope.write_text(envelope_text, encoding="utf-8")
            args = replace(
                args,
                authority_lines=(1, 5),
                authority_sha256=digest(generic.encode()),
                authority_envelope_sha256=digest(envelope_text.encode()),
            )
            with self.assertRaisesRegex(CloseError, "naming the exact target"):
                authority_binding(args)

            wrong_case = "Subject: Re: Close obsolete planner?\n\nClose MANAGER:0.\n"
            source.write_text(wrong_case, encoding="utf-8")
            envelope_text = f'<human_instruction authoritative="true" source="{locator}">\n{wrong_case}</human_instruction>\n'
            envelope.write_text(envelope_text, encoding="utf-8")
            args = replace(
                args,
                authority_lines=(1, 3),
                authority_sha256=digest(wrong_case.encode()),
                authority_envelope_sha256=digest(envelope_text.encode()),
            )
            with self.assertRaisesRegex(CloseError, "naming the exact target"):
                authority_binding(args)

            wrong_object = "Subject: Re: Close obsolete planner?\n\nClose other:0, not manager:0.\n"
            source.write_text(wrong_object, encoding="utf-8")
            envelope_text = f'<human_instruction authoritative="true" source="{locator}">\n{wrong_object}</human_instruction>\n'
            envelope.write_text(envelope_text, encoding="utf-8")
            args = replace(
                args,
                authority_sha256=digest(wrong_object.encode()),
                authority_envelope_sha256=digest(envelope_text.encode()),
            )
            with self.assertRaisesRegex(CloseError, "naming the exact target"):
                authority_binding(args)

            report_only = "Subject: Re: close helper\n\nI have asked another agent to close them, report this problem to the agent responsible\n"
            source.write_text(report_only, encoding="utf-8")
            envelope_text = f'<human_instruction authoritative="true" source="{locator}">\n{report_only}</human_instruction>\n'
            envelope.write_text(envelope_text, encoding="utf-8")
            args = replace(
                args,
                authority_lines=(1, 3),
                authority_sha256=digest(report_only.encode()),
                authority_envelope_sha256=digest(envelope_text.encode()),
            )
            with self.assertRaisesRegex(CloseError, "naming the exact target"):
                authority_binding(args)

    def test_guarded_close_announces_acceptance_before_killing_one_pane_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _args, _containment, _current, live = self.fixture(Path(tmp))
            expected = "OMO_MANAGER_SENTINEL_CLOSE_ACCEPTED_123_456"
            completed = __import__("subprocess").CompletedProcess([], 0, expected + "\n", "")
            with (
                patch("omo_manager.omo_manager_containment_close.os.getpid", return_value=123),
                patch("omo_manager.omo_manager_containment_close.time.monotonic_ns", return_value=456),
                patch("omo_manager.omo_manager_containment_close.subprocess.run", return_value=completed) as run,
            ):
                guarded_sentinel_close(live)

            command = run.call_args.args[0]
            self.assertEqual(["tmux", "if-shell", "-F", "-t"], command[:4])
            self.assertIn("#{==:#{window_panes},1}", close_condition(live))
            self.assertLess(command[6].index("display-message"), command[6].index("kill-pane"))
            self.assertIn(live.pane.pane_id, command[6])


if __name__ == "__main__":
    unittest.main()
