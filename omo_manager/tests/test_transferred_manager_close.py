from __future__ import annotations

# pyright: reportUninitializedInstanceVariable=false

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from typing import final
from unittest.mock import Mock, patch

from omo_manager import omo_transferred_manager_close as close_module
from omo_manager.omo_codex_stop import (
    bound_close_secret,
    done_live_close_started_path,
    has_bound_close_proof,
    promote_done_live_close_started,
    validate_bound_close_audit_file,
    write_done_live_close_started,
)
from omo_manager.omo_repository_custody import HeldAbsolute
from omo_manager.omo_task_metadata import TaskFrontmatterError, parse_task_metadata
from omo_manager.omo_transferred_manager_close import (
    AUTHORITY_SHA256,
    CLOSURE_ITEM,
    PanePin,
    abandon_prepared,
    canonical_packet,
    current_session_id,
    execute,
    prepare,
    stop_target,
    verify_live_session,
)

AUTHORITY = base64.b64decode(
    "U3ViamVjdDogUmU6IEJsb2NrZWQgaW5wdXQgcmVjb3Zlcnkg4oCUIGNsZWFudXBfZHdfdHJlZS5tZAoK"
    "Rm9yIG1hbmFnZXI6IHJlcGxhY2UgdGhpcyBzdHVwaWQgYWdlbnQuIHVuYmxvY2tpbmcgYSB0bXV4IHdp"
    "bmRvdyBpcyBuZXZlciB1bnNhZmUuIFRoZXkgYXJlIGJlaW5nIHN0dXBpZC4NCk9yLCBiZXR0ZXIgeWV0"
    "LCBpZiB0aGVyZSBpcyBub3QgcmVhbGx5IGEgbmVlZCBmb3IgdGhpcyBhZ2VudCwgc2ltcGx5IGNsb3Nl"
    "IHRoZW0uDQpBbHNvIGNsb3NlIGNvbmZpZzo3IGFuZCBjb25maWc6MiBhbmQgY29uZmlnOjMuIEkgZG9u"
    "4oCZdCBoZWFyIGFueXRoaW5nIGZyb20gdGhlbSwgc28gdGhleSBhcmUgbm90IGRvaW5nIHRoZWlyIGpv"
    "YnMNCg0KPiBPbiBTZXAgNywgMjAyNiwgYXQgMTc6MzEsIHNpY2hhbmdoZWFnZW50QGdtYWlsLmNvbSB3"
    "cm90ZToNCj4gDQo+IFRoZSBEZWVwV2lraSByZXBsYWNlbWVudCBpcyBibG9ja2VkIGJ5IGEgbGlmZWN5"
    "Y2xlIGlucHV0LXJlY292ZXJ5IGdhcC4gVHdvIGJsb2NrZWQgQ29kZXggcGFuZXMgY29udGFpbiBhbHJl"
    "YWR5LWtub3duIHF1ZXVlZCBtZXNzYWdlcywgYnV0IHRoZSBzdXBwb3J0ZWQgaGVscGVyIHJlZnVzZXMg"
    "Ym90aCBmaWxlLWJvdW5kIGFuZCBoYXNoLWJvdW5kIHJlY292ZXJ5IGJlY2F1c2UgdGhlaXIgaW5wdXQg"
    "ZW5kcyB3aXRoIGFuIGFtYmlndW91cyBibGFuayBsaW5lLiBGb3JjaW5nIEVudGVyIG9yIHN0b3BwaW5n"
    "IGVpdGhlciBsaXZlIHBhbmUgd291bGQgYmUgdW5zYWZlLCBzbyBJIHByZXNlcnZlZCB0aGVtLg0KPiAN"
    "Cj4gVGhlIGV4aXN0aW5nIHNvbGUgcmVwYWlyIG93bmVyIGF0IGNvbmZpZzo3IG5vdyBvd25zIHRoaXMg"
    "ZXhhY3QgaGVscGVyIGdhcCBhbG9uZ3NpZGUgdGhlIHJlcGxhY2VtZW50IHJlcGFpci4gTm8gbmV3IGxh"
    "bmUgd2FzIGNyZWF0ZWQuIFRoZSBuZXh0IGFjdGlvbiBpcyBhIHJldmlld2VkIGF1dGhlbnRpY2F0ZWQg"
    "cmVjb3ZlcnkgcGF0aCB0aGF0IGNsZWFycyBjb25maWc6MiB3aXRob3V0IHN1Ym1pc3Npb24gYW5kIHN1"
    "Ym1pdHMgdGhlIGNvbXBsZXRlIHdhdGNoZXIgcmVxdWVzdCBhdCBjb25maWc6MyB3aXRob3V0IHJlcGxh"
    "eSBvciBhbWJpZ3VpdHkuDQo+IA0KDQo="
)


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def task_text(
    status: str,
    target: str,
    manager: str,
    is_manager: bool,
    queue: tuple[str, ...],
    blocker: str = "",
) -> str:
    blocked = f"blocked_on: {blocker}\n" if blocker else ""
    pending = "pending_task_items: []\n" if not queue else "pending_task_items:\n" + "".join(f"  - {json.dumps(item, ensure_ascii=False)}\n" for item in queue)
    return f"---\nversion: v1.0.0\nstatus: {status}\n{blocked}runat: {target}\ntool: codex\nmanagerat: {manager}\nis_manager: {str(is_manager).lower()}\n{pending}---\nbody\n"


@final
class TransferredManagerCloseTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory[str]
    root: Path
    authority: Path
    prior_queue: tuple[str, ...]
    commit: str
    historical_sha: str
    source: str
    replacement: str
    stale: str
    child: str
    todo: str
    private: Path
    packet: Path
    audit: Path

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        trusted_root_patch = patch("omo_manager.omo_transferred_manager_close.trusted_root", return_value=self.root)
        trusted_root_patch.start()
        self.addCleanup(trusted_root_patch.stop)
        current_session_patch = patch(
            "omo_manager.omo_transferred_manager_close.current_session_id",
            return_value="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
        )
        current_session_patch.start()
        self.addCleanup(current_session_patch.stop)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        (self.root / "manager_mail").mkdir(mode=0o700)
        self.authority = self.root / "manager_mail" / "85c5dff58359-1492.txt"
        self.authority.write_bytes(AUTHORITY)
        self.authority.chmod(0o600)
        self.assertEqual(AUTHORITY_SHA256, digest(self.authority.read_bytes()))
        self.prior_queue = ("one", "two")
        historical = task_text("blocked", "config:3", "config:1", True, self.prior_queue, "dw_lpair.md")
        (self.root / "dw_tree_replace.md").write_text(historical)
        child = task_text("running", "worker:1", "config:3", False, ("child work",))
        second_child = task_text("running", "worker:2", "config:3", False, ("second child work",))
        (self.root / "dw_lpair.md").write_text(child)
        (self.root / "dw_rotate_exec.md").write_text(second_child)
        (self.root / "TODO.md").write_text("current:\ndw_lpair.md worker:1\ndw_rotate_exec.md worker:2\n\nhuman pending:\ndw_tree_replace.md config:3\n\nlow priority:\n\nprevious:\n")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "historical transfer source"], check=True)
        self.commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.historical_sha = digest(historical)
        self.source = task_text("blocked", "config:2", "config:1", True, (CLOSURE_ITEM,), "cleanup_dw_tree.md")
        self.replacement = task_text("long_running", "config:1", "wl:1", True, ("prefix", *self.prior_queue, "suffix"), "dw_tree_replace.md")
        self.stale = task_text("blocked", "config:2", "config:1", False, (), "dw_lpair.md") + "\n(manager closed Codex agent; tmux target `config:2`)\n"
        self.child = task_text("done", "worker:1", "config:1", False, ())
        (self.root / "dw_tree_replace.md").write_text(self.source)
        (self.root / "cleanup_dw_tree.md").write_text(self.replacement)
        (self.root / "dw_rotate_repair.md").write_text(self.stale)
        (self.root / "dw_lpair.md").write_text(self.child)
        (self.root / "dw_rotate_exec.md").write_text(self.child.replace("worker:1", "worker:2"))
        self.todo = "current:\ncleanup_dw_tree.md config:1\n\nhuman pending:\ndw_tree_replace.md config:2\n\nlow priority:\n\nprevious:\ndw_rotate_repair.md config:2\n"
        (self.root / "TODO.md").write_text(self.todo)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.packet = self.private / "packet.json"
        self.audit = self.private / "audit.json"

    def args(self) -> Namespace:
        return Namespace(
            root=self.root,
            source_task=Path("dw_tree_replace.md"),
            replacement_task=Path("cleanup_dw_tree.md"),
            stale_task=Path("dw_rotate_repair.md"),
            source_target="config:2",
            replacement_target="config:1",
            executor_target="config:4",
            source_sha256=digest(self.source),
            replacement_sha256=digest(self.replacement),
            stale_sha256=digest(self.stale),
            todo_sha256=digest(self.todo),
            historical_commit=self.commit,
            historical_source_sha256=self.historical_sha,
            authority=self.authority,
            authority_sha256=AUTHORITY_SHA256,
            authority_lines=(5, 5),
            source_pin=PanePin("config:2", "%2", 202, 2002),
            session_id="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
            protected_target=(PanePin("config:1", "%1", 101, 1001), PanePin("config:4", "%4", 404, 4004)),
            packet=self.packet,
            audit=self.audit,
        )

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_prepare_binds_transfer_and_same_target_records(self, _pane: object, _status: object, _pin: object) -> None:
        prepare(self.args())
        packet = json.loads(self.packet.read_text())
        self.assertEqual(digest(b'["one","two"]'), packet["historical_queue_sha256"])
        self.assertEqual(["dw_lpair.md", "dw_rotate_exec.md"], [item["task"] for item in packet["child_transfers"]])
        self.assertEqual(["config:1", "config:4"], [item["target"] for item in packet["protected_targets"]])
        self.assertEqual((5, 5), tuple(packet["authority_lines"]))

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_prepare_rejects_inferred_line_four_or_reordered_transfer(self, _pane: object, _status: object, _pin: object) -> None:
        args = self.args()
        args.authority_lines = (4, 4)
        with self.assertRaisesRegex(TaskFrontmatterError, "exactly name config:2"):
            prepare(args)
        args = self.args()
        changed = self.replacement.replace('  - "one"\n  - "two"', '  - "two"\n  - "one"')
        (self.root / "cleanup_dw_tree.md").write_text(changed)
        args.replacement_sha256 = digest(changed)
        with self.assertRaisesRegex(TaskFrontmatterError, "complete historical queue"):
            prepare(args)

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_prepare_rejects_child_reporting_drift(self, _pane: object, _status: object, _pin: object) -> None:
        (self.root / "dw_lpair.md").write_text(self.child.replace("managerat: config:1", "managerat: config:9"))
        with self.assertRaisesRegex(TaskFrontmatterError, "child reporting ownership"):
            prepare(self.args())

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_prepare_rejects_duplicate_protected_target(self, _pane: object, _status: object, _pin: object) -> None:
        args = self.args()
        args.protected_target = (*args.protected_target, args.protected_target[0])
        with self.assertRaisesRegex(TaskFrontmatterError, "exact config:2 source"):
            prepare(args)

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_prepare_rejects_competing_replacement_owner(self, _pane: object, _status: object, _pin: object) -> None:
        competing = task_text("running", "config:1", "wl:1", False, ("competing",))
        (self.root / "competing.md").write_text(competing)
        with self.assertRaisesRegex(TaskFrontmatterError, "sole active replacement owner"):
            prepare(self.args())

    @patch("omo_manager.omo_transferred_manager_close.current_session_id", return_value="01a07ef6-bc2b-7423-922b-27e20f15396a")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_prepare_rejects_live_session_drift(self, _pane: object, _status: object, _pin: object, _session: object) -> None:
        with self.assertRaisesRegex(TaskFrontmatterError, "session id differs"):
            prepare(self.args())

    @patch("omo_manager.omo_transferred_manager_close.query_status_session_id")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    def test_current_session_query_guards_source_and_protected_targets(self, pin: Mock, query: Mock) -> None:
        source = PanePin("config:2", "%2", 202, 2002)
        protected = (PanePin("config:1", "%1", 101, 1001), PanePin("config:4", "%4", 404, 4004))

        def answer(*_args: object, **kwargs: object) -> tuple[str, str]:
            self.assertTrue(kwargs["identity_is_current"]())
            kwargs["pre_input_check"]()
            return "01a07ef6-b5bd-7183-9c38-e780b51e7f2b", "status"

        query.side_effect = answer
        self.assertEqual("01a07ef6-b5bd-7183-9c38-e780b51e7f2b", current_session_id(source, protected))
        self.assertGreaterEqual(pin.call_count, 6)

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_verify_live_session_authenticates_exact_packet_binding(self, _pane: object, _status: object, _pin: object) -> None:
        prepare(self.args())
        verify_live_session(Namespace(packet=self.packet, packet_sha256=digest(self.packet.read_bytes())))

    @patch("omo_manager.omo_transferred_manager_close.current_session_id", return_value="01a07ef6-bc2b-7423-922b-27e20f15396a")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_abandon_prepared_records_stale_session_without_pane_or_task_mutation(
        self,
        _pane: object,
        _status: object,
        _pin: object,
        _session: object,
    ) -> None:
        with patch(
            "omo_manager.omo_transferred_manager_close.current_session_id",
            return_value="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
        ):
            prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        packet = json.loads(self.packet.read_text())
        prepared_path = Path(f"{self.audit}.prepared")
        prepared_path.write_bytes(close_module.prepared_audit_data(packet, packet_sha))
        prepared_path.chmod(0o600)
        drifted_todo = self.todo.replace("current:\n", "current:\nunrelated.md worker:9\n")
        (self.root / "TODO.md").write_text(drifted_todo)
        abandon_prepared(
            Namespace(
                packet=self.packet,
                packet_sha256=packet_sha,
                prepared_sha256=digest(prepared_path.read_bytes()),
            )
        )
        abandoned = Path(f"{prepared_path}.abandoned")
        record = json.loads(abandoned.read_text())
        self.assertEqual("abandoned-before-interrupt", record["state"])
        self.assertEqual("01a07ef6-bc2b-7423-922b-27e20f15396a", record["observed_session_id"])
        self.assertEqual(digest(drifted_todo), record["observed_todo_sha256"])
        self.assertEqual(digest(self.todo), record["packet_todo_sha256"])
        self.assertEqual(self.source, (self.root / "dw_tree_replace.md").read_text())
        self.assertEqual(self.stale, (self.root / "dw_rotate_repair.md").read_text())

    @patch("omo_manager.omo_transferred_manager_close.current_session_id", return_value="01a07ef6-bc2b-7423-922b-27e20f15396a")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_abandon_prepared_rejects_closure_todo_row_drift(
        self,
        _pane: object,
        _status: object,
        _pin: object,
        _session: object,
    ) -> None:
        with patch(
            "omo_manager.omo_transferred_manager_close.current_session_id",
            return_value="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
        ):
            prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        packet = json.loads(self.packet.read_text())
        prepared_path = Path(f"{self.audit}.prepared")
        prepared_path.write_bytes(close_module.prepared_audit_data(packet, packet_sha))
        prepared_path.chmod(0o600)
        (self.root / "TODO.md").write_text(self.todo.replace("dw_tree_replace.md config:2", "dw_tree_replace.md"))
        with self.assertRaisesRegex(TaskFrontmatterError, "source TODO custody changed"):
            abandon_prepared(
                Namespace(packet=self.packet, packet_sha256=packet_sha, prepared_sha256=digest(prepared_path.read_bytes()))
            )
        self.assertFalse(Path(f"{prepared_path}.abandoned").exists())

    @patch("omo_manager.omo_transferred_manager_close.current_session_id", return_value="01a07ef6-bc2b-7423-922b-27e20f15396a")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", side_effect=[True, True, True, True, False])
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_abandon_prepared_rejects_final_protected_target_race(
        self,
        _pane: object,
        _status: object,
        _pin: object,
        _session: object,
    ) -> None:
        with (
            patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True),
            patch(
                "omo_manager.omo_transferred_manager_close.current_session_id",
                return_value="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
            ),
        ):
            prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        packet = json.loads(self.packet.read_text())
        prepared_path = Path(f"{self.audit}.prepared")
        prepared_path.write_bytes(close_module.prepared_audit_data(packet, packet_sha))
        prepared_path.chmod(0o600)
        with self.assertRaisesRegex(TaskFrontmatterError, "changed before prepared disposition"):
            abandon_prepared(
                Namespace(packet=self.packet, packet_sha256=packet_sha, prepared_sha256=digest(prepared_path.read_bytes()))
            )
        self.assertFalse(Path(f"{prepared_path}.abandoned").exists())

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.current_session_id", return_value="01a07ef6-bc2b-7423-922b-27e20f15396a")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_abandoned_packet(
        self,
        _pane: object,
        _status: object,
        _pin: object,
        _session: object,
        _report: object,
    ) -> None:
        with patch(
            "omo_manager.omo_transferred_manager_close.current_session_id",
            return_value="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
        ):
            prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        packet = json.loads(self.packet.read_text())
        prepared_path = Path(f"{self.audit}.prepared")
        prepared_path.write_bytes(close_module.prepared_audit_data(packet, packet_sha))
        prepared_path.chmod(0o600)
        abandon_prepared(Namespace(packet=self.packet, packet_sha256=packet_sha, prepared_sha256=digest(prepared_path.read_bytes())))
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": close_module.REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "permanently abandoned"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.path_entry_exists", side_effect=[False, True])
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rechecks_abandonment_under_lifecycle_locks(
        self,
        _pane: object,
        _status: object,
        _pin: object,
        abandoned: Mock,
        _report: object,
    ) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": close_module.REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "permanently abandoned"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))
        self.assertEqual(2, abandoned.call_count)
        self.assertFalse(Path(f"{self.audit}.prepared").exists())

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.stop_target")
    @patch("omo_manager.omo_transferred_manager_close.current_session_id", return_value="01a07ef6-bc2b-7423-922b-27e20f15396a")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_live_session_drift_before_prepared_audit(
        self,
        _pane: object,
        _status: object,
        _pin: object,
        _session: object,
        stop: Mock,
        _report: object,
    ) -> None:
        with patch(
            "omo_manager.omo_transferred_manager_close.current_session_id",
            return_value="01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
        ):
            prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": close_module.REVIEW_SCHEMA,
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "session id differs before prepared audit"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))
        self.assertFalse(Path(f"{self.audit}.prepared").exists())
        stop.assert_not_called()

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_requires_review_to_bind_session(self, _pane: object, _status: object, _pin: object, _report: object) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n" + json.dumps({"schema": close_module.REVIEW_SCHEMA, "verdict": "PASS", "packet_sha256": packet_sha}).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "does not PASS"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))
        self.assertFalse(Path(f"{self.audit}.prepared").exists())

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.has_bound_close_proof", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.stop_target")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", side_effect=["%2", "%2", "%2", ""])
    def test_execute_closes_pane_and_both_records(
        self,
        _pane: Mock,
        _status: Mock,
        _pin: Mock,
        stop: Mock,
        _proof: Mock,
        _report: Mock,
    ) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": "omo-transferred-manager-close-review/v1",
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))
        stop.assert_called_once()
        self.assertEqual(
            ("config:2", "%2", 202, 2002, "01a07ef6-b5bd-7183-9c38-e780b51e7f2b"),
            stop.call_args.args[:5],
        )
        self.assertTrue(callable(stop.call_args.args[10]))
        for name in ("dw_tree_replace.md", "dw_rotate_repair.md"):
            result = parse_task_metadata((self.root / name).read_text(), self.root)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual("done", result.status)
            self.assertEqual((), result.pending_task_items)
        final_todo = (self.root / "TODO.md").read_text()
        self.assertIn("previous:\ndw_rotate_repair.md\ndw_tree_replace.md\n", final_todo)
        self.assertTrue(self.audit.is_file())

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:1"})
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_nonindependent_review(self, _pane: Mock, _status: Mock, _pin: Mock, _report: Mock) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": "omo-transferred-manager-close-review/v1",
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "not independent"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:4"})
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_executor_review(self, _pane: Mock, _status: Mock, _pin: Mock, _report: Mock) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": "omo-transferred-manager-close-review/v1",
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "not independent"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))

    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_packet_root_drift(self, _pane: Mock, _status: Mock, _pin: Mock) -> None:
        prepare(self.args())
        packet = json.loads(self.packet.read_text())
        packet["root"] = str(self.private)
        packet_without_binding = dict(packet)
        packet_without_binding.pop("binding_id")
        packet["binding_id"] = digest(canonical_packet(packet_without_binding))
        malicious = self.private / "malicious.json"
        malicious.write_bytes(canonical_packet(packet))
        review = self.private / "review.md"
        review.write_bytes(b"not reached")
        with self.assertRaisesRegex(TaskFrontmatterError, "exact config:2 transfer scope"):
            execute(
                Namespace(
                    packet=malicious,
                    packet_sha256=digest(malicious.read_bytes()),
                    review=review,
                    review_sha256=digest(review.read_bytes()),
                )
            )

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.stop_target")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_tampered_after_image_before_stop(
        self,
        _pane: Mock,
        _status: Mock,
        _pin: Mock,
        stop: Mock,
        _report: Mock,
    ) -> None:
        prepare(self.args())
        packet = json.loads(self.packet.read_text())
        packet["source_after_base64"] = base64.b64encode(b"arbitrary replacement\n").decode()
        packet["source_after_sha256"] = digest(b"arbitrary replacement\n")
        unbound = dict(packet)
        unbound.pop("binding_id")
        packet["binding_id"] = digest(canonical_packet(unbound))
        self.packet.write_bytes(canonical_packet(packet))
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": "omo-transferred-manager-close-review/v1",
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "before/after images"):
            execute(
                Namespace(
                    packet=self.packet,
                    packet_sha256=packet_sha,
                    review=review,
                    review_sha256=digest(review.read_bytes()),
                )
            )
        stop.assert_not_called()

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.has_bound_close_proof", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.stop_target")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", return_value=True)
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", side_effect=["%2", "%2", "%2", "", "", "", "", ""])
    def test_execute_recovers_after_stop_and_partial_metadata_write(
        self,
        _pane: Mock,
        _status: Mock,
        _pin: Mock,
        stop: Mock,
        _proof: Mock,
        _report: Mock,
    ) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": "omo-transferred-manager-close-review/v1",
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        execute_args = Namespace(
            packet=self.packet,
            packet_sha256=packet_sha,
            review=review,
            review_sha256=digest(review.read_bytes()),
        )
        writes = 0
        real_replace = close_module.replace_held

        def fail_second_write(held: HeldAbsolute, text: str) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("simulated metadata write failure")
            real_replace(held, text)

        with patch("omo_manager.omo_transferred_manager_close.replace_held", side_effect=fail_second_write):
            with self.assertRaisesRegex(OSError, "simulated metadata write failure"):
                execute(execute_args)
        self.assertTrue(Path(f"{self.audit}.prepared").is_file())
        execute(execute_args)
        stop.assert_called_once()
        for name in ("dw_tree_replace.md", "dw_rotate_repair.md"):
            result = parse_task_metadata((self.root / name).read_text(), self.root)
            assert result is not None
            self.assertEqual("done", result.status)
        self.assertTrue(self.audit.is_file())
        execute(execute_args)
        stop.assert_called_once()

    @patch("omo_manager.omo_transferred_manager_close.guarded_codex_stop")
    def test_stop_target_supplies_complete_guarded_close_contract(self, guarded_stop: Mock) -> None:
        session_id = "01a07ef6-b5bd-7183-9c38-e780b51e7f2b"
        secret = "a" * 64
        commitment = digest(secret)
        audit = self.private / "prepared.json"
        proof = audit.with_name(f".{audit.name}.owner-stopped")
        callback = Mock()
        guarded_stop.return_value = session_id
        audit_sha256 = "c" * 64
        stop_target("config:2", "%2", 202, 2002, session_id, proof, audit, secret, commitment, audit_sha256, callback)
        args = guarded_stop.call_args.args[0]
        self.assertEqual(str(proof), args.bound_close_proof_path)
        self.assertEqual(str(audit), args.bound_close_audit_path)
        self.assertEqual(secret, args.bound_close_proof_secret)
        self.assertEqual(commitment, args.bound_close_proof_commitment)
        self.assertIs(callback, args.bound_pre_input_check)
        self.assertEqual(session_id, args.bound_expected_session_id)
        self.assertEqual("transferred-manager-close", args.bound_close_operation)
        self.assertEqual(audit_sha256, args.bound_close_audit_sha256)

    def test_real_pre_kill_marker_requires_complete_audit_and_promotes_after_absence(self) -> None:
        secret = "b" * 64
        commitment = digest(secret)
        final_audit = self.private / "audit.json"
        audit = Path(f"{final_audit}.prepared")
        proof = audit.with_name(f".{audit.name}.owner-stopped")
        sha = "d" * 64
        record = {
            "schema": "omo-transferred-manager-close/v1",
            "root": "/ssd1/sichangheagent/work_logs",
            "source_task": "dw_tree_replace.md",
            "replacement_task": "cleanup_dw_tree.md",
            "stale_task": "dw_rotate_repair.md",
            "source_target": "config:2",
            "replacement_target": "config:1",
            "executor_target": "config:4",
            "close_proof_commitment": commitment,
            "source_sha256": sha,
            "replacement_sha256": sha,
            "stale_sha256": sha,
            "todo_sha256": sha,
            "historical_commit": "e" * 40,
            "historical_source_sha256": sha,
            "historical_queue_sha256": sha,
            "authority": "/ssd1/sichangheagent/work_logs/manager_mail/85c5dff58359-1492.txt",
            "authority_sha256": "399b09775312d638e3b57b35bdce85b9e8e1b6487528d35e3c0f2f5b80dbf4ec",
            "authority_lines": [5, 5],
            "source_pane": {"target": "config:2", "pane_id": "%2", "pane_pid": 202, "pane_start_ticks": 2002},
            "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
            "protected_targets": [
                {"target": "config:1", "pane_id": "%1", "pane_pid": 101, "pane_start_ticks": 1001},
                {"target": "config:4", "pane_id": "%4", "pane_pid": 404, "pane_start_ticks": 4004},
            ],
            "child_transfers": [
                {"task": "dw_lpair.md", "sha256": sha, "current_sha256": sha},
                {"task": "dw_rotate_exec.md", "sha256": sha, "current_sha256": sha},
            ],
            "source_after_sha256": sha,
            "stale_after_sha256": sha,
            "todo_after_sha256": sha,
            "audit": str(final_audit),
            "binding_id": sha,
            "packet_sha256": sha,
            "operation": "transferred-manager-close",
            "state": "prepared",
        }
        audit.write_bytes(canonical_packet(record))
        audit.chmod(0o600)
        audit_sha256 = digest(audit.read_bytes())
        validate_bound_close_audit_file("transferred-manager-close", audit, commitment, "config:2", "%2", 202, 2002, audit_sha256)
        started = write_done_live_close_started(
            proof,
            audit,
            secret,
            commitment,
            audit_sha256,
            "config:2",
            "%2",
            202,
            2002,
            "transferred-manager-close",
        )
        self.assertEqual(secret, bound_close_secret(started, commitment, audit_sha256, "transferred-manager-close"))
        with (
            patch("omo_manager.omo_codex_stop.pane_id", return_value=""),
            patch("omo_manager.omo_codex_stop.process_start_ticks", return_value=None),
        ):
            promote_done_live_close_started(
                proof,
                audit,
                commitment,
                audit_sha256,
                "config:2",
                "%2",
                202,
                2002,
                "transferred-manager-close",
            )
        self.assertTrue(has_bound_close_proof(proof, commitment, audit_sha256, "transferred-manager-close"))
        self.assertFalse(done_live_close_started_path(audit).exists())
        bad_audit = self.private / "bad-prepared.json"
        bad_record = dict(record)
        bad_record["executor_target"] = "config:8"
        bad_audit.write_bytes(canonical_packet(bad_record))
        bad_audit.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "drifted"):
            validate_bound_close_audit_file(
                "transferred-manager-close",
                bad_audit,
                commitment,
                "config:2",
                "%2",
                202,
                2002,
                digest(bad_audit.read_bytes()),
            )

    @patch("omo_manager.omo_transferred_manager_close.authenticated_report", return_value={"producer_target": "config:8"})
    @patch("omo_manager.omo_transferred_manager_close.stop_target")
    @patch("omo_manager.omo_transferred_manager_close.current_pin", side_effect=[True, True, True, False])
    @patch("omo_manager.omo_transferred_manager_close.inspect_target", return_value="ready")
    @patch("omo_manager.omo_transferred_manager_close.exact_pane_id", return_value="%2")
    def test_execute_rejects_protected_target_drift_before_stop(
        self,
        _pane: Mock,
        _status: Mock,
        _pin: Mock,
        stop: Mock,
        _report: Mock,
    ) -> None:
        prepare(self.args())
        packet_sha = digest(self.packet.read_bytes())
        review = self.private / "review.md"
        review.write_bytes(
            b"message:\n"
            + json.dumps(
                {
                    "schema": "omo-transferred-manager-close-review/v1",
                    "verdict": "PASS",
                    "packet_sha256": packet_sha,
                    "session_id": "01a07ef6-b5bd-7183-9c38-e780b51e7f2b",
                }
            ).encode()
        )
        with self.assertRaisesRegex(TaskFrontmatterError, "protected target identity changed"):
            execute(Namespace(packet=self.packet, packet_sha256=packet_sha, review=review, review_sha256=digest(review.read_bytes())))
        stop.assert_not_called()
        self.assertEqual(self.source, (self.root / "dw_tree_replace.md").read_text())
        self.assertEqual(self.stale, (self.root / "dw_rotate_repair.md").read_text())


if __name__ == "__main__":
    unittest.main()
