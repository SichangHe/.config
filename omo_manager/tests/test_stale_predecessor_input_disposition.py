from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_codex_stop, omo_stale_predecessor_input_disposition as subject
from omo_manager.omo_stale_predecessor_close import PanePin
from omo_manager.omo_task_metadata import TaskFrontmatterError

MENU_LINES = [
    "› /status",
    "",
    "  /status      show current session configuration and token usage",
    "  /statusline  configure which items appear in the status line",
]


def disposition_packet(tmp: Path) -> dict[str, object]:
    return {
        "root": str(tmp),
        "task": str(tmp / "worker.md"),
        "todo": str(tmp / "TODO.md"),
        "manager_task": str(tmp / "manager.md"),
        "manager_target": "dw:0",
        "predecessor_target": "dw8:0",
        "predecessor_pane": {"target": "dw8:0", "pane_id": "%1", "pane_pid": 101, "pane_start_ticks": 201},
        "predecessor_session_id": "01a07f0f-ffbd-7f13-89f1-4936c50be5c2",
        "protected_target": "dw8:1",
        "protected_pane": {"target": "dw8:1", "pane_id": "%2", "pane_pid": 102, "pane_start_ticks": 202},
        "protected_session_id": "01a07faa-011d-7983-86bb-978cf5a25169",
        "prior_packet_sha256": "1" * 64,
        "prepared_close_audit": str(tmp / "close.json.prepared"),
        "prepared_close_audit_sha256": "2" * 64,
        "execution_report_replay_id": "3" * 64,
        "recovery_report_replay_id": "4" * 64,
        "authorized_input": "/status",
        "input_authorization_sha256": "5" * 64,
        "menu_capture_sha256": "6" * 64,
        "binding_id": "7" * 64,
        "audit": str(tmp / "disposition.json"),
        "inputs": [],
    }


class StalePredecessorInputDispositionTests(unittest.TestCase):
    def execute_states(
        self,
        states: list[str],
        events: list[str],
        *,
        prepared_exists: bool,
        recoverable: bool = False,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet = disposition_packet(tmp)
            prepared_close = Path(str(packet["prepared_close_audit"]))
            prepared_close.write_bytes(b"preserved-close-audit\n")
            predecessor = PanePin("dw8:0", "%1", 101, 201)
            protected = PanePin("dw8:1", "%2", 102, 202)

            def live_state(
                _packet: dict[str, object],
                *,
                require_original_menu: bool,
                rebind_recoverable_helper: bool = False,
            ) -> tuple[PanePin, PanePin, str, bytes]:
                self.assertEqual(recoverable, rebind_recoverable_helper)
                state = states.pop(0)
                events.append(f"state:{state}:{require_original_menu}")
                return predecessor, protected, state, state.encode()

            def publish(path: Path, _data: bytes, label: str) -> None:
                events.append(f"publish:{label}:{path}")

            def hold_inputs(
                _packet: dict[str, object],
                _stack: contextlib.ExitStack,
                *,
                rebind_recoverable_helper: bool = False,
            ) -> list[object]:
                self.assertEqual(recoverable, rebind_recoverable_helper)
                return []

            def send_key(
                _pin: PanePin,
                command: list[str],
                *,
                validate_before: Callable[[], tuple[str, bytes]],
                validate_after: Callable[[], tuple[str, bytes]],
            ) -> tuple[str, bytes]:
                _state, capture = validate_before()
                events.append(f"key:{command[-1]}:{capture.decode()}")
                return validate_after()

            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="8" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="9" * 64,
                wait_s=0.01,
            )
            with (
                patch.object(subject, "read_bound", return_value=b"packet"),
                patch.object(subject, "validate_packet", return_value=packet),
                patch.object(subject, "validate_review"),
                patch.object(subject, "tmux_input_lock", side_effect=lambda _target: contextlib.nullcontext()),
                patch.object(subject, "task_target_lock", side_effect=lambda _root, _target: contextlib.nullcontext()),
                patch.object(subject, "task_file_lock", side_effect=lambda _path: contextlib.nullcontext()),
                patch.object(subject, "authorize_prepared_helper_recovery", return_value=recoverable),
                patch.object(subject, "hold_inputs", side_effect=hold_inputs),
                patch.object(subject, "path_entry_exists", side_effect=[prepared_exists, False]),
                patch.object(subject, "live_state", side_effect=live_state),
                patch.object(subject, "publish_or_validate", side_effect=publish),
                patch.object(subject, "guarded_tmux_command_for_capture", side_effect=send_key),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                subject.execute(args)
            self.assertEqual(b"preserved-close-audit\n", prepared_close.read_bytes())

    def test_exact_incomplete_status_menu_is_recognized(self) -> None:
        lines = ["older output", *MENU_LINES, "", ""]
        self.assertTrue(subject.exact_status_menu(lines))
        self.assertEqual("status_menu", subject.exact_recovery_state(lines))

    def test_menu_recognizer_rejects_any_expansion_or_drift(self) -> None:
        cases = (
            ["› /status now", *MENU_LINES[1:]],
            [MENU_LINES[0], "  /status      show current session configuration and token usage", MENU_LINES[-1]],
            [*MENU_LINES, "  /statusline extra"],
            [MENU_LINES[0], "", MENU_LINES[-1], MENU_LINES[-2]],
            [*MENU_LINES, "", "  gpt-5.5"],
        )
        for lines in cases:
            with self.subTest(lines=lines):
                self.assertFalse(subject.exact_status_menu(lines))
                self.assertEqual("other", subject.exact_recovery_state(lines))

    def test_bare_incomplete_status_input_is_not_authorized(self) -> None:
        self.assertEqual("other", subject.exact_recovery_state(["› /status"]))
        self.assertEqual("status_input", subject.exact_recovery_state(["› /status", "", "  gpt-5.5"]))
        self.assertEqual("ready", subject.exact_recovery_state(["› Ask Codex to do anything", "", "  gpt-5.5"]))

    def test_recovery_report_requirements_match_immutable_incident_wording(self) -> None:
        report_body = (
            "Supplemental dw8:0 recovery result consumed without a duplicate queue item. "
            "The supported exact `/status` cancellation dry-run validated, but live "
            "`--cancel-existing-file` failed before mutation with `target input is not in a "
            "complete Codex view`; fresh supported status remains `running` with the menu "
            "visible. This supplies no safe-stop evidence. I will not retry, force keys, "
            "regenerate a packet, or touch dw8:0/dw8:1 until supported status is stably ready "
            "or a narrow independently reviewed provenance-safe disposition is released. "
            "Both panes and the prepared audit remain preserved.\n"
        )
        required_text = (
            "dw8:0",
            *subject.RECOVERY_REPORT_REQUIRED_TEXT,
            "dw8:1",
        )
        self.assertTrue(all(token in report_body for token in required_text))
        self.assertNotIn("not safe-stop evidence", report_body)

    def test_execute_dismisses_menu_then_cancels_only_status_input(self) -> None:
        events: list[str] = []
        self.execute_states(["status_menu", "status_menu", "status_input", "status_input", "ready", "ready"], events, prepared_exists=False)
        self.assertEqual(
            ["key:Escape:status_menu", "key:C-c:status_input"],
            [event for event in events if event.startswith("key:")],
        )
        self.assertEqual(
            ["prepared input disposition audit", "complete input disposition audit"],
            [event.split(":", 2)[1] for event in events if event.startswith("publish:")],
        )
        prepared_index = next(index for index, event in enumerate(events) if event.startswith("publish:prepared"))
        escape_index = next(index for index, event in enumerate(events) if event.startswith("key:Escape:"))
        self.assertLess(prepared_index, escape_index)

    def test_prepared_recovery_completes_from_ready_without_keys(self) -> None:
        events: list[str] = []
        self.execute_states(["ready", "ready"], events, prepared_exists=True)
        self.assertFalse(any(event.startswith("key:") for event in events))
        self.assertEqual(
            ["prepared input disposition audit", "complete input disposition audit"],
            [event.split(":", 2)[1] for event in events if event.startswith("publish:")],
        )

    def test_exact_prepared_recovery_rebinds_helper_and_completes_without_keys(self) -> None:
        events: list[str] = []
        self.execute_states(["ready", "ready"], events, prepared_exists=True, recoverable=True)
        self.assertFalse(any(event.startswith("key:") for event in events))
        self.assertEqual(
            ["prepared input disposition audit", "complete input disposition audit"],
            [event.split(":", 2)[1] for event in events if event.startswith("publish:")],
        )

    def test_prepared_helper_recovery_requires_all_exact_incident_hashes(self) -> None:
        packet: dict[str, object] = {}
        prepared = b"exact prepared audit\n"
        prepared_path = Path("/tmp/exact-prepared-audit")
        prepared_sha256 = subject.sha256(prepared)
        with (
            patch.object(subject, "is_recoverable_prepared_packet", return_value=True),
            patch.object(subject, "RECOVERABLE_PREPARED_AUDIT_SHA256", prepared_sha256),
            patch.object(subject, "read_bound", return_value=prepared) as read,
        ):
            self.assertTrue(
                subject.authorize_prepared_helper_recovery(
                    packet,
                    subject.RECOVERABLE_PACKET_SHA256,
                    subject.RECOVERABLE_REVIEW_SHA256,
                    prepared_path,
                    prepared,
                )
            )
            read.assert_called_once_with(
                prepared_path,
                prepared_sha256,
                "recoverable prepared disposition audit",
                private=True,
            )
            for packet_sha256, review_sha256 in (
                ("0" * 64, subject.RECOVERABLE_REVIEW_SHA256),
                (subject.RECOVERABLE_PACKET_SHA256, "0" * 64),
            ):
                with self.subTest(packet_sha256=packet_sha256, review_sha256=review_sha256):
                    self.assertFalse(
                        subject.authorize_prepared_helper_recovery(
                            packet,
                            packet_sha256,
                            review_sha256,
                            prepared_path,
                            prepared,
                        )
                    )

    def test_prepared_helper_recovery_holds_current_helper_in_place_of_old_binding(self) -> None:
        helper = Path(subject.__file__).resolve(strict=True)
        old_helper_input = subject.file_input(helper, "input disposition helper")
        old_helper_file = subject.object_map(old_helper_input["file"], "old helper")
        old_helper_file["sha256"] = subject.RECOVERABLE_HELPER_SHA256
        old_helper_input["file"] = old_helper_file
        packet: dict[str, object] = {
            "helper": str(helper),
            "helper_sha256": subject.RECOVERABLE_HELPER_SHA256,
            "inputs": [old_helper_input],
        }
        with (
            contextlib.ExitStack() as stack,
            patch.object(subject, "is_recoverable_prepared_packet", return_value=True),
        ):
            held = subject.hold_inputs(packet, stack, rebind_recoverable_helper=True)
            self.assertEqual(1, len(held))
            subject.validate_held_absolute(held[0])
            self.assertEqual(subject.sha256(helper.read_bytes()), held[0].identity.sha256)
            self.assertNotEqual(subject.RECOVERABLE_HELPER_SHA256, held[0].identity.sha256)

    def test_menu_race_before_escape_fails_without_key(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_menu", "other"], events, prepared_exists=False)
        self.assertFalse(any(event.startswith("key:") for event in events))
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_state_race_after_escape_never_sends_control_c(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_menu", "status_menu", "other"], events, prepared_exists=False)
        self.assertEqual(["key:Escape:status_menu"], [event for event in events if event.startswith("key:")])
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_ready_race_after_cancel_never_publishes_complete(self) -> None:
        events: list[str] = []
        with self.assertRaisesRegex(TaskFrontmatterError, "unsupported state"):
            self.execute_states(["status_input", "status_input", "ready", "status_input"], events, prepared_exists=True)
        self.assertEqual(["key:C-c:status_input"], [event for event in events if event.startswith("key:")])
        self.assertFalse(any(event.startswith("publish:complete") for event in events))

    def test_post_action_binding_failure_prevents_a_second_key_and_complete(self) -> None:
        events: list[str] = []

        def changed_session(
            _packet: dict[str, object],
            *,
            require_original_menu: bool,
            rebind_recoverable_helper: bool = False,
        ) -> tuple[PanePin, PanePin, str, bytes]:
            self.assertFalse(rebind_recoverable_helper)
            calls = sum(event.startswith("state:") for event in events)
            events.append(f"state:{calls}:{require_original_menu}")
            if calls == 2:
                raise TaskFrontmatterError("predecessor or protected Codex session changed.")
            state = "status_menu"
            return PanePin("dw8:0", "%1", 101, 201), PanePin("dw8:1", "%2", 102, 202), state, state.encode()

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet = disposition_packet(tmp)
            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="8" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="9" * 64,
                wait_s=0.01,
            )

            def send_key(
                _pin: PanePin,
                command: list[str],
                *,
                validate_before: Callable[[], tuple[str, bytes]],
                validate_after: Callable[[], tuple[str, bytes]],
            ) -> tuple[str, bytes]:
                _state, capture = validate_before()
                events.append(f"key:{command[-1]}:{capture.decode()}")
                return validate_after()

            with (
                patch.object(subject, "read_bound", return_value=b"packet"),
                patch.object(subject, "validate_packet", return_value=packet),
                patch.object(subject, "validate_review"),
                patch.object(subject, "tmux_input_lock", side_effect=lambda _target: contextlib.nullcontext()),
                patch.object(subject, "task_target_lock", side_effect=lambda _root, _target: contextlib.nullcontext()),
                patch.object(subject, "task_file_lock", side_effect=lambda _path: contextlib.nullcontext()),
                patch.object(subject, "hold_inputs", return_value=[]),
                patch.object(subject, "path_entry_exists", side_effect=[False, False]),
                patch.object(subject, "live_state", side_effect=changed_session),
                patch.object(subject, "publish_or_validate"),
                patch.object(subject, "guarded_tmux_command_for_capture", side_effect=send_key),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(TaskFrontmatterError, "session changed"),
            ):
                subject.execute(args)
        self.assertEqual(["key:Escape:status_menu"], [event for event in events if event.startswith("key:")])

    def test_capture_guard_compares_exact_fresh_bytes_in_one_tmux_queue(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        capture = "older output\n› /status\n\n  /status      show current session configuration and token usage\n  /statusline  configure which items appear in the status line\n".encode()
        observed: list[list[str]] = []

        def guarded_sequence(
            target: str,
            pane_id: str,
            commands: list[list[str]],
            pane_pid: int,
        ) -> str:
            self.assertEqual((pin.target, pin.pane_id, pin.pane_pid), (target, pane_id, pane_pid))
            observed.extend(commands)
            return f"OMO_DISPOSITION_CAPTURE_ACCEPTED_{'a' * 32}\n"

        with (
            patch.object(subject.secrets, "token_hex", return_value="a" * 32),
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|50\n", "", "", ""]),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(subject, "guarded_tmux_sequence", side_effect=guarded_sequence),
        ):
            state, after_capture = subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", capture),
                validate_after=lambda: ("status_input", b"after"),
            )
        self.assertEqual(("status_input", b"after"), (state, after_capture))

        option = f"@omo-disposition-capture-{'a' * 32}"
        self.assertEqual("if-shell", observed[0][0])
        self.assertEqual(["-F", "-t", pin.pane_id], observed[0][1:4])
        self.assertIn("PANE_IDENTITY", observed[0][4])
        self.assertIn("#{==:#{pane_dead},0}", observed[0][4])
        self.assertIn("#{==:#{pane_current_command},bunx}", observed[0][4])
        self.assertIn("#{==:#{buffer-limit},50}", observed[0][4])
        self.assertIn(f"set-option -p -o -t %1 {option}", observed[0][5])
        self.assertIn(f"set-option -g buffer-limit {subject.TEMPORARY_TMUX_BUFFER_LIMIT}", observed[0][5])
        self.assertIn("capture-pane -J -N -t %1", observed[0][5])
        self.assertIn("set-option -g buffer-limit 50", observed[0][5])
        self.assertIn(f"#{{==:#{{buffer_full}},#{{{option}}}}}", observed[0][5])
        self.assertIn("send-keys -t %1 Escape", observed[0][5])
        self.assertLess(observed[0][5].index("delete-buffer"), observed[0][5].index("send-keys"))
        self.assertNotIn("send-keys", observed[0][6])
        self.assertEqual(1, len(observed))

    def test_capture_guard_rejects_drift_and_non_cancellation_commands(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.secrets, "token_hex", return_value="a" * 32),
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|50\n", "", "", ""]),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                return_value=f"OMO_DISPOSITION_CAPTURE_REJECTED_{'a' * 32}\n",
            ) as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "capture changed"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "C-c"],
                validate_before=lambda: ("status_input", b"reviewed capture"),
                validate_after=lambda: ("ready", b"ready"),
            )
        guarded.assert_called_once()

        with (
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "cancellation-only"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Enter"],
                validate_before=lambda: ("status_input", b"reviewed capture"),
                validate_after=lambda: ("ready", b"ready"),
            )
        guarded.assert_not_called()

        buffers = "1\n" * 95
        with (
            patch.object(subject.secrets, "token_hex", return_value="b" * 32),
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|50\n", buffers, "", ""]),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                return_value=f"OMO_DISPOSITION_CAPTURE_ACCEPTED_{'b' * 32}\n",
            ) as guarded,
        ):
            result = subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", b"reviewed capture"),
                validate_after=lambda: ("status_input", b"status input"),
            )
        self.assertEqual(("status_input", b"status input"), result)
        command = guarded.call_args.args[2][0][5]
        self.assertIn(f"set-option -g buffer-limit {subject.TEMPORARY_TMUX_BUFFER_LIMIT}", command)
        guarded.assert_called_once()

    def test_capture_guard_fails_closed_when_inventory_exceeds_bound(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", "1\n" * (subject.MAX_TMUX_BUFFER_INVENTORY + 1)],
            ),
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "inventory exceeds"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: self.fail("capture validation must not run"),
                validate_after=lambda: self.fail("post-validation must not run"),
            )
        guarded.assert_not_called()

    def test_capture_guard_fails_closed_before_overwriting_existing_option(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.secrets, "token_hex", return_value="c" * 32),
            patch.object(
                subject,
                "bound_guarded_read",
                side_effect=["bunx|0|50\n", "", "existing option\n"],
            ),
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "guard option already exists"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: self.fail("capture validation must not run"),
                validate_after=lambda: self.fail("post-validation must not run"),
            )
        guarded.assert_not_called()

    def test_capture_guard_attempts_owned_state_restoration_after_tmux_timeout(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.secrets, "token_hex", return_value="d" * 32),
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|50\n", "", "", ""]),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                side_effect=subprocess.TimeoutExpired(["tmux", "if-shell"], 5),
            ),
            patch.object(subject, "restore_temporary_capture_state") as restore,
            self.assertRaisesRegex(TaskFrontmatterError, "did not complete"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", b"reviewed capture"),
                validate_after=lambda: ("status_menu", b"reviewed capture"),
            )
        restore.assert_called_once_with(
            pin,
            lease_option=f"@omo-disposition-buffer-limit-{'d' * 32}",
            capture_option=f"@omo-disposition-capture-{'d' * 32}",
            original_buffer_limit="50",
        )

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the format integration test")
    def test_capture_guard_runs_against_supported_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory) / "tmux.sock")
            tmux = ["tmux", "-S", socket]

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*tmux, *args],
                    capture_output=True,
                    check=check,
                    text=True,
                    timeout=5,
                )

            run("new-session", "-d", "-s", "guard", "-x", "80", "-y", "24", "sleep 30")
            try:
                run("set-option", "-g", "buffer-limit", "100")
                for index in range(95):
                    run("set-buffer", f"preserved-{index}")
                run("set-option", "-g", "buffer-limit", "50")
                before_buffers = run(
                    "list-buffers",
                    "-F",
                    "#{buffer_name}|#{buffer_size}|#{buffer_full}",
                ).stdout
                self.assertEqual(95, len(before_buffers.splitlines()))
                identity = run(
                    "display-message",
                    "-p",
                    "-t",
                    "guard:0.0",
                    "#{pane_id}|#{pane_pid}",
                ).stdout.strip()
                pane_id, raw_pid = identity.split("|", 1)
                pin = PanePin("guard:0.0", pane_id, int(raw_pid), 1)
                capture = run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode()

                def isolated_tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*tmux, *args],
                        capture_output=True,
                        check=check,
                        text=True,
                        timeout=5,
                    )

                with patch.object(omo_codex_stop, "tmux", side_effect=isolated_tmux):
                    state, _after = subject.guarded_tmux_command_for_capture(
                        pin,
                        ["send-keys", "-t", pane_id, "Escape"],
                        validate_before=lambda: ("status_menu", capture),
                        validate_after=lambda: ("status_input", capture),
                    )
                    self.assertEqual("status_input", state)
                    self.assertEqual("50", run("show-option", "-gv", "buffer-limit").stdout.strip())
                    self.assertEqual(
                        before_buffers,
                        run("list-buffers", "-F", "#{buffer_name}|#{buffer_size}|#{buffer_full}").stdout,
                    )
                    with self.assertRaisesRegex(TaskFrontmatterError, "capture changed"):
                        subject.guarded_tmux_command_for_capture(
                            pin,
                            ["send-keys", "-t", pane_id, "Escape"],
                            validate_before=lambda: ("status_menu", b"not the current capture"),
                            validate_after=lambda: ("status_input", capture),
                        )
                    self.assertEqual("50", run("show-option", "-gv", "buffer-limit").stdout.strip())
                    self.assertEqual(
                        before_buffers,
                        run("list-buffers", "-F", "#{buffer_name}|#{buffer_size}|#{buffer_full}").stdout,
                    )
            finally:
                run("kill-server", check=False)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the failure integration test")
    def test_capture_guard_restores_real_server_after_mid_queue_capture_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory) / "tmux.sock")
            tmux = ["tmux", "-S", socket]

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*tmux, *args],
                    capture_output=True,
                    check=check,
                    text=True,
                    timeout=5,
                )

            run("new-session", "-d", "-s", "guard", "-x", "80", "-y", "24", "cat")
            run("set-option", "-g", "remain-on-exit", "on")
            try:
                run("set-option", "-g", "buffer-limit", "100")
                for index in range(95):
                    run("set-buffer", f"preserved-{index}")
                run("set-option", "-g", "buffer-limit", "50")
                before_buffers = run(
                    "list-buffers",
                    "-F",
                    "#{buffer_name}|#{buffer_size}|#{buffer_full}",
                ).stdout
                pane_id, raw_pid = (
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        "guard:0.0",
                        "#{pane_id}|#{pane_pid}",
                    )
                    .stdout.strip()
                    .split("|", 1)
                )
                pin = PanePin("guard:0.0", pane_id, int(raw_pid), 1)
                capture = run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode()
                original_sequence = subject.guarded_tmux_sequence

                def isolated_tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*tmux, *args],
                        capture_output=True,
                        check=check,
                        text=True,
                        timeout=5,
                    )

                def failed_capture_sequence(
                    target: str,
                    expected_pane_id: str,
                    commands: list[list[str]],
                    expected_pane_pid: int = 0,
                ) -> str:
                    mutated = [list(command) for command in commands]
                    capture_command = f"capture-pane -J -N -t {pane_id}"
                    failed_command = "capture-pane -J -N -t %999999999"
                    self.assertEqual(1, mutated[0][5].count(capture_command))
                    mutated[0][5] = mutated[0][5].replace(capture_command, failed_command)
                    return original_sequence(
                        target,
                        expected_pane_id,
                        mutated,
                        expected_pane_pid,
                    )

                with (
                    patch.object(omo_codex_stop, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "guarded_tmux_sequence", side_effect=failed_capture_sequence),
                    self.assertRaisesRegex(TaskFrontmatterError, "did not complete"),
                ):
                    subject.guarded_tmux_command_for_capture(
                        pin,
                        ["send-keys", "-t", pane_id, "C-c"],
                        validate_before=lambda: ("status_input", capture),
                        validate_after=lambda: ("status_input", capture),
                    )
                self.assertEqual("50", run("show-option", "-gv", "buffer-limit").stdout.strip())
                self.assertEqual(
                    before_buffers,
                    run("list-buffers", "-F", "#{buffer_name}|#{buffer_size}|#{buffer_full}").stdout,
                )
                self.assertNotIn(
                    "@omo-disposition-",
                    run("show-options", "-p", "-t", pane_id).stdout,
                )
                self.assertNotIn("@omo-disposition-", run("show-options", "-s").stdout)
                self.assertEqual(
                    "0|cat",
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        pane_id,
                        "#{pane_dead}|#{pane_current_command}",
                    ).stdout.strip(),
                )
            finally:
                run("kill-server", check=False)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for the race integration test")
    def test_capture_guard_rejects_real_output_race_without_key_or_buffer_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory) / "tmux.sock")
            tmux = ["tmux", "-S", socket]

            def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [*tmux, *args],
                    capture_output=True,
                    check=check,
                    text=True,
                    timeout=5,
                )

            run("new-session", "-d", "-s", "guard", "-x", "80", "-y", "24", "cat")
            run("set-option", "-g", "remain-on-exit", "on")
            try:
                pane_id, raw_pid = (
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        "guard:0.0",
                        "#{pane_id}|#{pane_pid}",
                    )
                    .stdout.strip()
                    .split("|", 1)
                )
                pin = PanePin("guard:0.0", pane_id, int(raw_pid), 1)
                capture = run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode()
                original_sequence = subject.guarded_tmux_sequence

                def isolated_tmux(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                    return subprocess.run(
                        [*tmux, *args],
                        capture_output=True,
                        check=check,
                        text=True,
                        timeout=5,
                    )

                def raced_sequence(
                    target: str,
                    expected_pane_id: str,
                    commands: list[list[str]],
                    expected_pane_pid: int = 0,
                ) -> str:
                    run("send-keys", "-t", pane_id, "-l", "capture-race")
                    run("send-keys", "-t", pane_id, "Enter")
                    time.sleep(0.05)
                    self.assertNotEqual(
                        capture,
                        run("capture-pane", "-p", "-J", "-N", "-t", pane_id).stdout.encode(),
                    )
                    return original_sequence(
                        target,
                        expected_pane_id,
                        commands,
                        expected_pane_pid,
                    )

                with (
                    patch.object(omo_codex_stop, "tmux", side_effect=isolated_tmux),
                    patch.object(subject, "guarded_tmux_sequence", side_effect=raced_sequence),
                    self.assertRaisesRegex(TaskFrontmatterError, "capture changed"),
                ):
                    subject.guarded_tmux_command_for_capture(
                        pin,
                        ["send-keys", "-t", pane_id, "C-c"],
                        validate_before=lambda: ("status_input", capture),
                        validate_after=lambda: ("ready", capture),
                    )
                time.sleep(0.05)
                self.assertEqual(
                    "0|cat",
                    run(
                        "display-message",
                        "-p",
                        "-t",
                        pane_id,
                        "#{pane_dead}|#{pane_current_command}",
                    ).stdout.strip(),
                )
                self.assertEqual("", run("list-buffers", "-F", "#{buffer_name}").stdout)
            finally:
                run("kill-server", check=False)

    def test_prior_close_control_paths_are_reserved_for_prepare_and_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            prior_complete = tmp / "close.json"
            prior_prepared = tmp / "close.json.prepared"
            controls = subject.prior_close_control_paths(prior_complete, prior_prepared)
            self.assertEqual(
                {
                    prior_complete,
                    prior_prepared,
                    tmp / ".close.json.prepared.owner-close-started",
                    tmp / ".close.json.prepared.owner-stopped",
                },
                controls,
            )
            for path in controls:
                with self.subTest(path=path, role="packet"), self.assertRaisesRegex(TaskFrontmatterError, "overlap"):
                    subject.validate_disposition_output_paths(path, tmp / "disposition.json", controls)
                with self.subTest(path=path, role="audit"), self.assertRaisesRegex(TaskFrontmatterError, "overlap"):
                    subject.validate_disposition_output_paths(tmp / "packet.json", path, controls)
            with self.assertRaisesRegex(TaskFrontmatterError, "overlap"):
                subject.validate_disposition_output_paths(
                    tmp / "disposition.json.prepared",
                    tmp / "disposition.json",
                    controls,
                )

            args = argparse.Namespace(
                packet=tmp / "packet.json",
                packet_sha256="8" * 64,
                review_report=tmp / "review.json",
                review_report_sha256="9" * 64,
                wait_s=0.01,
            )
            for path in controls:
                packet = disposition_packet(tmp)
                packet["prepared_close_audit"] = str(prior_prepared)
                packet["audit"] = str(path)
                with (
                    self.subTest(path=path, role="execute"),
                    patch.object(subject, "read_bound", return_value=b"packet"),
                    patch.object(subject, "validate_packet", return_value=packet),
                    patch.object(subject, "validate_review"),
                    patch.object(subject, "publish_or_validate") as publish,
                    self.assertRaisesRegex(TaskFrontmatterError, "overlap"),
                ):
                    subject.execute(args)
                publish.assert_not_called()

    def test_complete_audit_retains_prior_prepared_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packet = disposition_packet(Path(directory))
            record = json.loads(subject.complete_disposition_audit(packet, "8" * 64, "9" * 64))
        self.assertEqual(packet["prepared_close_audit"], record["prepared_close_audit"])
        self.assertEqual(packet["prepared_close_audit_sha256"], record["prepared_close_audit_sha256"])
        self.assertEqual("ready", record["final_state"])
        self.assertIn("prior close packet superseded", record["result"])


if __name__ == "__main__":
    unittest.main()
