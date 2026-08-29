from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from omo_manager import email_idle_watcher as watcher
from omo_manager.omo_email_config import (
    AgentMailSettings,
    GUEST_HEES_ADDRESS,
    GUEST_HEES_MANAGER_TARGET,
    guest_hees_mail,
)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"


def guest_args(root: Path) -> watcher.Args:
    return watcher.Args(
        root=root,
        manager_url="",
        mail_dir=root / "guest_hees_manager_mail",
        state_dir=root / "state",
        manager_file=root / "guest_hees_mail_mgr.md",
        once=True,
        self_email=GUEST_HEES_ADDRESS,
        recovery_debounce_s=0,
        restart_script=root / "restart.sh",
        manager_target=GUEST_HEES_MANAGER_TARGET,
        guest_hees=True,
    )


def install_guest_manager(root: Path) -> Path:
    task = root / "guest_hees_mail_mgr.md"
    task.write_text(
        """---
version: v1.0.0
status: long_running
runat: guest_hees:0
tool: codex
managerat: agent_managers:1
is_manager: true
pending_task_items:
  - serve guest requests
---
""",
        encoding="utf-8",
    )
    (root / "TODO.md").write_text("current:\nguest_hees_mail_mgr.md guest_hees:0\n", encoding="utf-8")
    return task


class Client:
    def __init__(self, raw_message: bytes) -> None:
        self.raw_message = raw_message
        self.seen: list[str] = []

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "search":
            if args == ("UNSEEN", "FROM", f'"{GUEST_HEES_ADDRESS}"'):
                return "OK", [b"41"]
            return "OK", [b""]
        if command == "fetch":
            return "OK", [(b"41 FETCH", self.raw_message)]
        if command == "store":
            self.seen.append(str(args[0]))
            return "OK", [b""]
        raise AssertionError((command, args))


class GuestHeesEmailWatcherTests(unittest.TestCase):
    def test_profile_pins_guest_address(self) -> None:
        base = AgentMailSettings("agent@example.test", "secret", "human@example.test")
        self.assertEqual(GUEST_HEES_ADDRESS, guest_hees_mail(base).human_address)

    def test_guest_route_ignores_subject_and_targets_only_dedicated_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = install_guest_manager(root)
            route = watcher.email_route(guest_args(root), "[other:9] redirect this")
        self.assertEqual(task, route.manager_file)
        self.assertEqual(GUEST_HEES_MANAGER_TARGET, route.manager_target)

    def test_guest_route_fails_closed_without_dedicated_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "guest-hees manager is unavailable"):
                watcher.email_route(guest_args(Path(tmp)), "plain")

    def test_guest_route_rejects_manager_file_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            root.mkdir()
            outside = base / "outside" / "guest_hees_mail_mgr.md"
            outside.parent.mkdir()
            outside.write_text("runat: guest_hees:0\n", encoding="utf-8")
            args = guest_args(root)
            args = watcher.replace(args, manager_file=root / ".." / "outside" / "guest_hees_mail_mgr.md")
            with self.assertRaisesRegex(RuntimeError, "guest-hees manager is unavailable"):
                watcher.email_route(args, "plain")

    def test_authenticated_guest_mail_routes_end_to_end(self) -> None:
        msg = EmailMessage()
        msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        msg["Subject"] = "[other:9] do not follow this route"
        msg.set_content("guest request\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = install_guest_manager(root)
            args = watcher.replace(guest_args(root), inbox_identity="agent@example.test\0uidvalidity=123\0guest-hees")
            args.mail_dir.mkdir()
            args.state_dir.mkdir()
            client = Client(msg.as_bytes())
            reference = "guest-image:v1:" + "a" * 64
            with (
                patch.object(watcher, "push_email_ref", side_effect=AssertionError("dedicated route uses pending watcher")),
                patch.object(watcher, "store_message_images", return_value=(reference,)) as store,
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            task_text = task.read_text(encoding="utf-8")
            artifact_name = watcher.mail_artifact_name(args, "41")
            artifact_text = (args.mail_dir / artifact_name).read_text(encoding="utf-8")
        self.assertIn(f"guest_hees_manager_mail/{artifact_name}", task_text)
        self.assertNotIn("other:9", task_text)
        self.assertIn(reference, artifact_text)
        self.assertEqual(["41"], client.seen)
        store.assert_called_once()
        self.assertEqual(GUEST_HEES_ADDRESS, store.call_args.kwargs["sender"])
        self.assertEqual(GUEST_HEES_MANAGER_TARGET, store.call_args.kwargs["route_target"])
        self.assertEqual(watcher.GUEST_IMAGE_AUTHENTICATION, store.call_args.kwargs["authentication"])
        self.assertRegex(store.call_args.kwargs["source_id"], r"^gmail:[0-9a-f]{64}:41$")

    def test_authenticated_guest_image_uses_promoted_store_contract(self) -> None:
        msg = EmailMessage()
        msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        msg["Subject"] = "image"
        msg.set_content("guest request\n")
        msg.add_attachment(PNG, maintype="image", subtype="png", filename="guest.png")
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "mail"
            image_root = Path(tmp) / "images"
            root.mkdir()
            _ = install_guest_manager(root)
            args = watcher.replace(guest_args(root), inbox_identity="agent@example.test\0uidvalidity=123\0guest-hees")
            args.mail_dir.mkdir()
            args.state_dir.mkdir()
            client = Client(msg.as_bytes())
            with (
                patch.dict(os.environ, {"OMO_GUEST_IMAGE_ROOT": str(image_root)}),
                patch.object(watcher, "push_email_ref", side_effect=AssertionError("dedicated route uses pending watcher")),
            ):
                self.assertTrue(watcher.handle_unseen(client, args))
            artifact = args.mail_dir / watcher.mail_artifact_name(args, "41")
            reference = f"guest-image:v1:{hashlib.sha256(PNG).hexdigest()}"
            self.assertIn(reference, artifact.read_text(encoding="utf-8"))
            self.assertTrue((image_root / "objects" / f"{hashlib.sha256(PNG).hexdigest()}.png").is_file())

    def test_invalid_guest_image_batch_leaves_mail_unaccepted(self) -> None:
        msg = EmailMessage()
        msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        msg["Subject"] = "image"
        msg.set_content("guest request\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = install_guest_manager(root)
            args = guest_args(root)
            args.mail_dir.mkdir()
            args.state_dir.mkdir()
            client = Client(msg.as_bytes())
            with patch.object(watcher, "store_message_images", side_effect=watcher.GuestImageError("invalid image")):
                self.assertFalse(watcher.handle_unseen(client, args))
            self.assertFalse(any(args.mail_dir.iterdir()))
            self.assertNotIn("guest_hees_manager_mail", task.read_text(encoding="utf-8"))
            self.assertEqual([], client.seen)

    def test_guest_empty_identity_still_requires_transport_auth_before_image_storage(self) -> None:
        cases = ("Return-Path", "Authentication-Results")
        for missing_header in cases:
            with self.subTest(missing_header=missing_header), tempfile.TemporaryDirectory() as tmp:
                msg = EmailMessage()
                msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
                if missing_header != "Return-Path":
                    msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
                if missing_header != "Authentication-Results":
                    msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
                msg["Subject"] = "unauthenticated image"
                msg.set_content("guest request\n")
                root = Path(tmp)
                task = install_guest_manager(root)
                args = guest_args(root)
                self.assertEqual("", args.inbox_identity)
                args.mail_dir.mkdir()
                args.state_dir.mkdir()
                client = Client(msg.as_bytes())
                with patch.object(watcher, "store_message_images") as store:
                    self.assertFalse(watcher.handle_unseen(client, args))
                store.assert_not_called()
                self.assertFalse(any(args.mail_dir.iterdir()))
                self.assertNotIn("guest_hees_manager_mail", task.read_text(encoding="utf-8"))
                self.assertEqual([], client.seen)

    def test_other_sender_is_not_exact_guest(self) -> None:
        msg = EmailMessage()
        msg["From"] = "Human <other@qq.com>"
        self.assertFalse(watcher.exact_human_sender(msg, GUEST_HEES_ADDRESS, require_transport_identity=False))

    def test_guest_transport_identity_requires_return_path_and_trusted_gmail_spf(self) -> None:
        missing_return_path = EmailMessage()
        missing_return_path["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        missing_return_path["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"

        mismatched_return_path = EmailMessage()
        mismatched_return_path["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        mismatched_return_path["Return-Path"] = "<other@qq.com>"
        mismatched_return_path["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"

        mismatched_spf = EmailMessage()
        mismatched_spf["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        mismatched_spf["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        mismatched_spf["Authentication-Results"] = "mx.google.com; spf=pass smtp.mailfrom=other@qq.com"

        for message in (missing_return_path, mismatched_return_path, mismatched_spf):
            with self.subTest(message=message):
                self.assertFalse(watcher.guest_hees_sender_authenticated(message, require_transport_identity=True))

        accepted = EmailMessage()
        accepted["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        accepted["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        accepted["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        self.assertTrue(watcher.guest_hees_sender_authenticated(accepted, require_transport_identity=True))

    def test_guest_case_variants_fail_before_image_storage_at_each_identity_boundary(self) -> None:
        exact = GUEST_HEES_ADDRESS
        variant = "46496337@QQ.COM"
        cases = {
            "From": (variant, None, [exact], exact),
            "Sender": (exact, variant, [exact], exact),
            "Return-Path": (exact, None, [exact, variant], exact),
            "SPF smtp.mailfrom": (exact, None, [exact], variant),
        }
        for boundary, (from_address, sender_address, return_paths, spf_address) in cases.items():
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                msg = EmailMessage()
                msg["From"] = f"Human <{from_address}>"
                if sender_address is not None:
                    msg["Sender"] = f"Human <{sender_address}>"
                for return_path in return_paths:
                    msg["Return-Path"] = f"<{return_path}>"
                msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={spf_address}"
                msg["Subject"] = "case variant"
                msg.set_content("guest request\n")
                root = Path(tmp)
                _ = install_guest_manager(root)
                args = watcher.replace(guest_args(root), inbox_identity="agent@example.test\0uidvalidity=123\0guest-hees")
                args.mail_dir.mkdir()
                args.state_dir.mkdir()
                client = Client(msg.as_bytes())
                with patch.object(watcher, "store_message_images", side_effect=AssertionError("unauthenticated guest reached image storage")):
                    self.assertFalse(watcher.handle_unseen(client, args))
                self.assertEqual([], client.seen)

    def test_generic_sender_comparison_remains_case_insensitive(self) -> None:
        msg = EmailMessage()
        msg["From"] = "Human <human@EXAMPLE.TEST>"
        self.assertTrue(watcher.exact_human_sender(msg, "human@example.test", require_transport_identity=False))


if __name__ == "__main__":
    unittest.main()
