from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from omo_manager.omo_config22_terminal_close import (
    AUDIT18,
    AUDIT19,
    AUTHORITY_SHA256,
    EXECUTOR,
    REPLAY_ID,
    TARGET,
    PanePin,
    classify_close_state,
    execute,
    existing_committed_audit,
    hold_prepared_proof,
    prepared_proof_exists,
    validate_audits,
    validate_committed_recovery,
    validate_executor,
    validate_pin,
    validate_protected,
    validate_review,
    validate_task,
)
from omo_manager.omo_repository_custody import CustodyError, validate_held_absolute
from omo_manager.omo_task_metadata import TaskFrontmatterError


class Config22TerminalCloseTests(TestCase):
    def test_audits_require_exact_serial_committed_chain(self) -> None:
        audit18 = {
            "schema": "omo-config18-satisfied-close/v1",
            "state": "committed",
            "audit": str(AUDIT18),
            "task": "config16_close.md",
            "task_after_sha256": "0e70ff352e1d0ec200a71f97c677dffdbef905715783aba0ace8f7c1a1ac1652",
            "authority_sha256": AUTHORITY_SHA256,
            "replay_id": REPLAY_ID,
            "manager_target": EXECUTOR,
        }
        audit19 = {
            "schema": "omo-config19-satisfied-close/v1",
            "state": "committed",
            "audit": str(AUDIT19),
            "task": "dw2_wrap_cancel.md",
            "task_after_sha256": "4f82c1eb376a3d257b9409717e03974f48dea2f1bb5e9eaf20ccc8a2e37487c2",
            "authority_sha256": AUTHORITY_SHA256,
            "replay_id": REPLAY_ID,
            "manager_target": EXECUTOR,
            "upstream_audit": str(AUDIT18),
            "upstream_audit_sha256": "0894057cd73b451d277d2731aafb9300b41e79a646394d230bcc61dcdd96106d",
        }
        validate_audits(json.dumps(audit18).encode(), json.dumps(audit19).encode())
        audit19["upstream_audit_sha256"] = "0" * 64
        with self.assertRaises(TaskFrontmatterError):
            validate_audits(json.dumps(audit18).encode(), json.dumps(audit19).encode())

    def test_task_requires_done_empty_and_one_previous_row(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            task = root / "config16_preflight.md"
            source = """---
version: v1.0.0
status: done
runat: config:22
tool: codex
managerat: wl:12
is_manager: false
pending_task_items: []
---
terminal
"""
            todo = b"current:\n\nhuman pending:\n\nlow priority:\n\nprevious:\nconfig16_preflight.md config:22\n"
            with patch(
                "omo_manager.omo_config22_terminal_close.authoritative_active_target_task_paths",
                return_value=(),
            ):
                validate_task(root, task, source.encode(), todo)
                with self.assertRaises(TaskFrontmatterError):
                    validate_task(root, task, source.encode(), todo + b"config16_preflight.md config:22\n")

    def test_ready_and_identity_are_rechecked(self) -> None:
        pin = PanePin(TARGET, "%1307", 3362440, 41351900, "01a087e9-bd82-7923-92cd-dbff1d2cf95f")
        with (
            patch("omo_manager.omo_config22_terminal_close.target_identity", return_value=(pin.pane_id, pin.pane_pid, pin.pane_start_ticks)),
            patch("omo_manager.omo_config22_terminal_close.session_from_process", return_value=pin.session_id),
            patch("omo_manager.omo_config22_terminal_close.inspect_target", return_value="ready"),
        ):
            validate_pin(pin, require_ready=True)
        with (
            patch("omo_manager.omo_config22_terminal_close.target_identity", return_value=(pin.pane_id, pin.pane_pid, pin.pane_start_ticks)),
            patch("omo_manager.omo_config22_terminal_close.session_from_process", return_value=pin.session_id),
            patch("omo_manager.omo_config22_terminal_close.inspect_target", return_value="running"),
            self.assertRaises(TaskFrontmatterError),
        ):
            validate_pin(pin, require_ready=True)

    def test_executor_rejects_non_wl21_before_packet_read(self) -> None:
        with (
            patch.dict("os.environ", {"TMUX_PANE": "%77"}),
            patch("omo_manager.omo_config22_terminal_close.exact_pane_id", return_value="%77"),
            patch("omo_manager.omo_config22_terminal_close.target_identity", return_value=("%77", 700, 900)),
            patch("omo_manager.omo_config22_terminal_close.process_is_under", return_value=True),
        ):
            validate_executor()
        ns = argparse.Namespace(packet=Path("missing"), packet_sha256="0" * 64)
        with (
            patch.dict("os.environ", {"TMUX_PANE": "%76"}),
            patch("omo_manager.omo_config22_terminal_close.exact_pane_id", return_value="%77"),
            patch("omo_manager.omo_config22_terminal_close.target_identity", return_value=("%77", 700, 900)),
            patch("omo_manager.omo_config22_terminal_close.process_is_under", return_value=True),
            patch("omo_manager.omo_config22_terminal_close.read_regular") as read,
            self.assertRaises(TaskFrontmatterError),
        ):
            execute(ns)
        read.assert_not_called()

        with (
            patch.dict("os.environ", {"TMUX_PANE": "%77"}),
            patch("omo_manager.omo_config22_terminal_close.exact_pane_id", return_value="%77"),
            patch("omo_manager.omo_config22_terminal_close.target_identity", return_value=("%77", 700, 900)),
            patch("omo_manager.omo_config22_terminal_close.process_is_under", return_value=False),
            self.assertRaises(TaskFrontmatterError),
        ):
            validate_executor()

    def test_review_must_be_exact_canonical_pass(self) -> None:
        packet_sha = "1" * 64
        expected = {
            "schema": "omo-config22-terminal-close-review/v1",
            "verdict": "PASS",
            "packet_sha256": packet_sha,
        }
        with patch(
            "omo_manager.omo_config22_terminal_close.authenticated_report",
            return_value={"producer_target": "config:21"},
        ):
            validate_review(b"header\nmessage:\n" + json.dumps(expected).encode(), packet_sha)
            with self.assertRaises(TaskFrontmatterError):
                validate_review(b"header\nmessage:\n" + json.dumps({**expected, "verdict": "BLOCK"}).encode(), packet_sha)
        with (
            patch(
                "omo_manager.omo_config22_terminal_close.authenticated_report",
                return_value={"producer_target": TARGET},
            ),
            self.assertRaises(TaskFrontmatterError),
        ):
            validate_review(b"header\nmessage:\n" + json.dumps(expected).encode(), packet_sha)

    def test_protected_drift_and_recovery_fail_closed(self) -> None:
        expected = [{"target": "config:20"}, {"target": "dw2:0"}]
        with patch("omo_manager.omo_config22_terminal_close.protected_snapshots", return_value=expected):
            validate_protected(expected, "drift")
        with (
            patch("omo_manager.omo_config22_terminal_close.protected_snapshots", return_value=[]),
            self.assertRaisesRegex(TaskFrontmatterError, "drift"),
        ):
            validate_protected(expected, "drift")
        validate_committed_recovery(("", 0, 0), True)
        with self.assertRaises(TaskFrontmatterError):
            validate_committed_recovery(("%1307", 3362440, 41351900), True)
        with self.assertRaises(TaskFrontmatterError):
            validate_committed_recovery(("", 0, 0), False)

    def test_committed_audit_without_prior_prepared_proof_stays_rejected(self) -> None:
        with TemporaryDirectory() as raw:
            audit = Path(raw) / "audit.json"
            committed = b'{"state":"committed"}\n'
            audit.write_bytes(committed)
            with self.assertRaises(TaskFrontmatterError):
                existing_committed_audit(audit, committed, ("", 0, 0), Path(raw) / "missing", b"prepared")
            self.assertFalse((Path(raw) / "audit.json.prepared").exists())
            prepared = Path(raw) / "audit.json.prepared"
            prepared.write_bytes(b"prepared")
            prepared.chmod(0o600)
            self.assertTrue(existing_committed_audit(audit, committed, ("", 0, 0), prepared, b"prepared"))

    def test_prepared_proof_is_rechecked_after_deletion(self) -> None:
        with TemporaryDirectory() as raw:
            proof = Path(raw) / "audit.json.prepared"
            expected = b'{"state":"prepared"}\n'
            proof.write_bytes(expected)
            proof.chmod(0o600)
            self.assertTrue(prepared_proof_exists(proof, expected))
            proof.unlink()
            self.assertFalse(prepared_proof_exists(proof, expected))
            with self.assertRaises(TaskFrontmatterError):
                validate_committed_recovery(("", 0, 0), prepared_proof_exists(proof, expected))

    def test_absent_recovery_never_recreates_deleted_prepared_proof(self) -> None:
        pin = PanePin(TARGET, "%1307", 3362440, 41351900, "01a087e9-bd82-7923-92cd-dbff1d2cf95f")
        with TemporaryDirectory() as raw:
            proof = Path(raw) / "audit.json.prepared"
            prepared = b'{"state":"prepared"}\n'
            proof.write_bytes(prepared)
            proof.chmod(0o600)
            self.assertFalse(classify_close_state(pin, ("", 0, 0), proof, prepared))
            proof.unlink()
            with self.assertRaises(TaskFrontmatterError):
                classify_close_state(pin, ("", 0, 0), proof, prepared)
            self.assertFalse(proof.exists())

    def test_held_prepared_proof_detects_deletion_before_commit(self) -> None:
        with TemporaryDirectory() as raw:
            proof = Path(raw) / "audit.json.prepared"
            prepared = b'{"state":"prepared"}\n'
            proof.write_bytes(prepared)
            proof.chmod(0o600)
            held = hold_prepared_proof(proof, prepared)
            try:
                proof.unlink()
                with self.assertRaises((CustodyError, FileNotFoundError)):
                    validate_held_absolute(held)
            finally:
                os.close(held.descriptor)
                for descriptor in reversed(held.directories):
                    os.close(descriptor)
