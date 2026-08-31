from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from queue import Empty
from unittest.mock import patch

from omo_manager import omo_pending_watch as watcher
from omo_manager.omo_email_config import GuestHeesOwner
from omo_manager.omo_email_config import guest_hees_intake_is_delivered


class GuestHeesPendingDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        while True:
            try:
                watcher.DELIVERY_SUCCESS_EVENTS.get_nowait()
            except Empty:
                break

    def args(self, root: Path, state_dir: Path) -> watcher.Args:
        return watcher.Args(
            root,
            "",
            state_dir / "pending-watch-seen.tsv",
            1,
            1,
            1,
            root / "status.sh",
            True,
            False,
            manager_target="primary:1",
        )

    def marker(self, task_file: str = "guest.md") -> watcher.Marker:
        block = "(pending)\n(record and delegate guest_hees_manager_mail/request.txt)"
        return watcher.Marker(
            Path(task_file),
            1,
            "d" * 64,
            "human",
            "email",
            "guest_hees_manager_mail/request.txt",
            block,
            block,
            2,
            "",
        )

    def test_same_target_replacement_redelivers_and_rebinds_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            state_dir.mkdir()
            owner_a = GuestHeesOwner(root / "owner-a.md", "guest_hees:7")
            owner_b = GuestHeesOwner(root / "owner-b.md", "guest_hees:7")
            marker = self.marker()
            self.assertTrue(watcher.record_guest_hees_intake_delivery(state_dir, marker.delegate_source, owner_a))
            self.assertTrue(guest_hees_intake_is_delivered(state_dir, marker.delegate_source, owner_a))
            self.assertFalse(guest_hees_intake_is_delivered(state_dir, marker.delegate_source, owner_b))
            with (
                patch.object(watcher, "active_guest_hees_owner", return_value=owner_b),
                patch.object(watcher, "guest_hees_owner_is_current", return_value=True),
                patch.object(
                    watcher,
                    "push_marker_delivery",
                    return_value=watcher.DeliveryResult(watcher.ASYNC_DELIVERY_STARTED),
                ) as deliver,
            ):
                status = watcher.push_guest_hees_ref(
                    self.args(root, state_dir),
                    {},
                    100,
                    marker,
                    (watcher.SourceAttachment(marker.delegate_source, "new request"),),
                )
            self.assertEqual(watcher.ASYNC_DELIVERY_STARTED, status)
            self.assertEqual(owner_b.target, deliver.call_args.args[3])
            event = deliver.call_args.args[4]
            self.assertEqual(owner_b, event.guest_owner)
            watcher.DELIVERY_SUCCESS_EVENTS.put(event)
            with (
                patch.object(watcher, "guest_hees_owner_is_current", return_value=True),
                patch.object(watcher, "clear_pending_marker_if_current", return_value=True),
                patch.object(watcher, "prune_report_authorities"),
                patch.object(watcher, "drain_send_results"),
            ):
                watcher.drain_delivery_successes(self.args(root, state_dir), {}, 101)
            self.assertFalse(guest_hees_intake_is_delivered(state_dir, marker.delegate_source, owner_a))
            self.assertTrue(guest_hees_intake_is_delivered(state_dir, marker.delegate_source, owner_b))

    def test_owner_change_before_paste_rejects_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            owner = GuestHeesOwner(root / "owner-a.md", "guest_hees:7")
            guard = watcher.PendingGuard(root, Path("guest.md"), 1, "d" * 64, "(pending)", owner)

            def invoke_before_paste(_target: str, _message: str, _options: object, *, before_paste: object) -> None:
                assert callable(before_paste)
                before_paste()

            with (
                patch.object(watcher, "pending_marker_present", return_value=True),
                patch.object(watcher, "guest_hees_owner_is_current", return_value=False),
                patch.object(watcher, "verified_send_to_codex", side_effect=invoke_before_paste),
                self.assertRaisesRegex(watcher.PrePasteRejected, "owner changed"),
            ):
                watcher.run_verified_send(owner.target, "request", watcher.CodexSendOptions(1, 0, False), guard)

    def test_owner_change_after_paste_retains_marker_and_writes_no_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            state_dir.mkdir()
            owner = GuestHeesOwner(root / "owner-a.md", "guest_hees:7")
            marker = self.marker()
            watcher.DELIVERY_SUCCESS_EVENTS.put(
                watcher.DeliverySuccessEvent(
                    clear_root=root,
                    clear_marker=marker,
                    guest_owner=owner,
                    guest_source=marker.delegate_source,
                )
            )
            with (
                patch.object(watcher, "guest_hees_owner_is_current", return_value=False),
                patch.object(watcher, "record_guest_hees_intake_delivery") as record,
                patch.object(watcher, "clear_pending_marker_if_current") as clear,
                patch.object(watcher, "prune_report_authorities"),
                patch.object(watcher, "drain_send_results"),
            ):
                watcher.drain_delivery_successes(self.args(root, state_dir), {}, 100)
            record.assert_not_called()
            clear.assert_not_called()

    def test_guest_delivery_failure_has_no_primary_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            state_dir.mkdir()
            owner = GuestHeesOwner(root / "owner.md", "guest_hees:7")
            marker = self.marker()
            with (
                patch.object(watcher, "active_guest_hees_owner", return_value=owner),
                patch.object(watcher, "guest_hees_owner_is_current", return_value=True),
                patch.object(watcher, "push_marker_delivery", return_value=watcher.DeliveryResult(1, "failed")) as deliver,
                patch.object(watcher, "clear_pending_marker_if_current") as clear,
            ):
                status = watcher.push_guest_hees_ref(
                    self.args(root, state_dir),
                    {},
                    100,
                    marker,
                    (watcher.SourceAttachment(marker.delegate_source, "request"),),
                )
            self.assertEqual(1, status)
            self.assertEqual({"guest_owner": owner}, deliver.call_args.kwargs)
            clear.assert_not_called()

    def test_synchronous_delivery_records_owner_before_marker_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            state_dir.mkdir()
            owner = GuestHeesOwner(root / "owner.md", "guest_hees:7")
            marker = self.marker()
            events: list[str] = []
            original_record = watcher.record_guest_hees_intake_delivery

            def record(*args: object) -> bool:
                events.append("receipt")
                return original_record(*args)

            def clear(*_args: object) -> bool:
                events.append("clear")
                return True

            with (
                patch.object(watcher, "active_guest_hees_owner", return_value=owner),
                patch.object(watcher, "guest_hees_owner_is_current", return_value=True),
                patch.object(watcher, "push_marker_delivery", return_value=watcher.DeliveryResult(0)),
                patch.object(watcher, "record_guest_hees_intake_delivery", side_effect=record),
                patch.object(watcher, "clear_pending_marker_if_current", side_effect=clear),
            ):
                status = watcher.push_guest_hees_ref(
                    self.args(root, state_dir),
                    {},
                    100,
                    marker,
                    (watcher.SourceAttachment(marker.delegate_source, "request"),),
                )
            self.assertEqual(0, status)
            self.assertEqual(["receipt", "clear"], events)
            self.assertTrue(guest_hees_intake_is_delivered(state_dir, marker.delegate_source, owner))

    def test_synchronous_delivery_retains_marker_when_receipt_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            state_dir.mkdir()
            owner = GuestHeesOwner(root / "owner.md", "guest_hees:7")
            marker = self.marker()
            with (
                patch.object(watcher, "active_guest_hees_owner", return_value=owner),
                patch.object(watcher, "guest_hees_owner_is_current", return_value=True),
                patch.object(watcher, "push_marker_delivery", return_value=watcher.DeliveryResult(0)),
                patch.object(watcher, "record_guest_hees_intake_delivery", return_value=False),
                patch.object(watcher, "clear_pending_marker_if_current") as clear,
            ):
                status = watcher.push_guest_hees_ref(
                    self.args(root, state_dir),
                    {},
                    100,
                    marker,
                    (watcher.SourceAttachment(marker.delegate_source, "request"),),
                )
            self.assertEqual(1, status)
            clear.assert_not_called()

    def test_attachment_failure_and_non_guest_markers_stay_contained(self) -> None:
        marker = self.marker()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            state_dir.mkdir()
            with patch.object(watcher, "active_guest_hees_owner") as resolve:
                self.assertEqual(
                    1,
                    watcher.push_guest_hees_ref(
                        self.args(root, state_dir),
                        {},
                        100,
                        marker,
                        (watcher.SourceAttachment(marker.delegate_source, "", error="unreadable"),),
                    ),
                )
            resolve.assert_not_called()
        ordinary = watcher.replace(marker, delegate_source="manager_mail/request.txt")
        pb = watcher.replace(marker, origin="agent", source="agent")
        self.assertFalse(watcher.guest_hees_email_marker(ordinary))
        self.assertFalse(watcher.guest_hees_email_marker(pb))


if __name__ == "__main__":
    unittest.main()
