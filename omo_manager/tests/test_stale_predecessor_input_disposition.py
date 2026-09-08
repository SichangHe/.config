from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
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
    def execute_states(self, states: list[str], events: list[str], *, prepared_exists: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            packet = disposition_packet(tmp)
            prepared_close = Path(str(packet["prepared_close_audit"]))
            prepared_close.write_bytes(b"preserved-close-audit\n")
            predecessor = PanePin("dw8:0", "%1", 101, 201)
            protected = PanePin("dw8:1", "%2", 102, 202)

            def live_state(_packet: dict[str, object], *, require_original_menu: bool) -> tuple[PanePin, PanePin, str, bytes]:
                state = states.pop(0)
                events.append(f"state:{state}:{require_original_menu}")
                return predecessor, protected, state, state.encode()

            def publish(path: Path, _data: bytes, label: str) -> None:
                events.append(f"publish:{label}:{path}")

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
                patch.object(subject, "hold_inputs", return_value=[]),
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
        ) -> tuple[PanePin, PanePin, str, bytes]:
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
            return "OMO_DISPOSITION_CAPTURE_ACCEPTED_123-456\n"

        with (
            patch.object(subject.os, "getpid", return_value=123),
            patch.object(subject.time, "monotonic_ns", return_value=456),
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|50\n", ""]),
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

        option = "@omo-disposition-capture-123-456"
        self.assertEqual(["set-option", "-p", "-t", pin.pane_id, option, capture.decode()], observed[0])
        self.assertEqual(["capture-pane", "-J", "-N", "-t", pin.pane_id], observed[1])
        self.assertEqual("if-shell", observed[2][0])
        self.assertIn("PANE_IDENTITY", observed[2][4])
        self.assertIn("#{==:#{pane_dead},0}", observed[2][4])
        self.assertIn("#{==:#{pane_current_command},bunx}", observed[2][4])
        self.assertIn(f"#{{==:#{{buffer_full}},#{{{option}}}}}", observed[2][4])
        self.assertIn("send-keys -t %1 Escape", observed[2][5])
        self.assertLess(observed[2][5].index("delete-buffer"), observed[2][5].index("send-keys"))
        self.assertIn(f"set-option -p -u -t %1 {option}", observed[2][5])
        self.assertNotIn("send-keys", observed[2][6])
        self.assertEqual(3, len(observed))

    def test_capture_guard_rejects_drift_and_non_cancellation_commands(self) -> None:
        pin = PanePin("dw8:0", "%1", 101, 201)
        with (
            patch.object(subject.os, "getpid", return_value=123),
            patch.object(subject.time, "monotonic_ns", return_value=456),
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|50\n", ""]),
            patch.object(subject, "tmux_guard_condition", return_value="PANE_IDENTITY"),
            patch.object(
                subject,
                "guarded_tmux_sequence",
                return_value="OMO_DISPOSITION_CAPTURE_REJECTED_123-456\n",
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

        with (
            patch.object(subject, "bound_guarded_read", side_effect=["bunx|0|1\n", "buffer0001\n"]),
            patch.object(subject, "guarded_tmux_sequence") as guarded,
            self.assertRaisesRegex(TaskFrontmatterError, "non-destructive automatic capture-buffer slot"),
        ):
            subject.guarded_tmux_command_for_capture(
                pin,
                ["send-keys", "-t", pin.pane_id, "Escape"],
                validate_before=lambda: ("status_menu", b"reviewed capture"),
                validate_after=lambda: ("status_input", b"status input"),
            )
        guarded.assert_not_called()

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
                    with self.assertRaisesRegex(TaskFrontmatterError, "capture changed"):
                        subject.guarded_tmux_command_for_capture(
                            pin,
                            ["send-keys", "-t", pane_id, "Escape"],
                            validate_before=lambda: ("status_menu", b"not the current capture"),
                            validate_after=lambda: ("status_input", capture),
                        )
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
