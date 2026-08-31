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
    fulfill_guest_hees_reply_obligation,
    guest_hees_mail,
)

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"
GUEST_TARGET = "guest_hees:7"


def guest_args(root: Path) -> watcher.Args:
    return watcher.Args(
        root=root,
        manager_url="",
        mail_dir=root / "guest_hees_manager_mail",
        state_dir=root / "state",
        manager_file=None,
        once=True,
        self_email=GUEST_HEES_ADDRESS,
        recovery_debounce_s=0,
        restart_script=root / "restart.sh",
        manager_target="",
        guest_hees=True,
    )


def install_guest_manager(root: Path, *, target: str = GUEST_TARGET, status: str = "long_running", name: str = "guest_current_mgr.md") -> Path:
    task = root / name
    task.write_text(
        f"""---
version: v1.0.0
status: {status}
runat: {target}
tool: codex
managerat: agent_managers:1
is_manager: true
pending_task_items:
  - serve guest requests
---
""",
        encoding="utf-8",
    )
    (root / "TODO.md").write_text(f"current:\n{name} {target}\n", encoding="utf-8")
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
    def setUp(self) -> None:
        self.sendable = patch("omo_manager.omo_tmux_send.require_sendable_codex_target", return_value=None)
        self.sendable.start()

    def tearDown(self) -> None:
        self.sendable.stop()

    def test_profile_pins_guest_address(self) -> None:
        base = AgentMailSettings("agent@example.test", "secret", "human@example.test")
        self.assertEqual(GUEST_HEES_ADDRESS, guest_hees_mail(base).human_address)

    def test_guest_route_ignores_subject_and_targets_only_dedicated_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = install_guest_manager(root)
            route = watcher.email_route(guest_args(root), "[other:9] redirect this")
        self.assertEqual(task, route.manager_file)
        self.assertEqual(GUEST_TARGET, route.manager_target)

    def test_guest_route_fails_closed_without_dedicated_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "requires exactly one"):
                watcher.email_route(guest_args(Path(tmp)), "plain")

    def test_guest_route_fails_closed_for_inactive_or_duplicate_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = install_guest_manager(root, status="done")
            with self.assertRaisesRegex(RuntimeError, "requires exactly one"):
                watcher.email_route(guest_args(root), "plain")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = install_guest_manager(root)
            second = install_guest_manager(root, target="guest_hees:8", name="second_mgr.md")
            (root / "TODO.md").write_text(
                f"current:\nguest_current_mgr.md {GUEST_TARGET}\n{second.name} guest_hees:8\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                watcher.email_route(guest_args(root), "plain")

    def test_guest_route_fails_closed_for_unsendable_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = install_guest_manager(root)
            with patch("omo_manager.omo_tmux_send.require_sendable_codex_target", side_effect=RuntimeError("missing pane")):
                with self.assertRaisesRegex(RuntimeError, "not sendable"):
                    watcher.email_route(guest_args(root), "plain")

    def test_authenticated_guest_mail_routes_end_to_end(self) -> None:
        msg = EmailMessage()
        msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        msg["Message-ID"] = "<guest-request@example.test>"
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
            obligation_text = next((args.state_dir / "guest-hees-reply-obligations").glob("*.state")).read_text(encoding="utf-8")
        self.assertIn(f"guest_hees_manager_mail/{artifact_name}", task_text)
        self.assertNotIn("other:9", task_text)
        self.assertIn(reference, artifact_text)
        self.assertEqual([], client.seen)
        store.assert_called_once()
        self.assertEqual(GUEST_HEES_ADDRESS, store.call_args.kwargs["sender"])
        self.assertEqual(GUEST_TARGET, store.call_args.kwargs["route_target"])
        self.assertIn("Message-ID: <guest-request@example.test>", artifact_text)
        self.assertIn("status=open", obligation_text)
        self.assertEqual(watcher.GUEST_IMAGE_AUTHENTICATION, store.call_args.kwargs["authentication"])
        self.assertRegex(store.call_args.kwargs["source_id"], r"^gmail:[0-9a-f]{64}:41$")

    def test_fulfilled_uid_is_seen_only_after_verified_reply(self) -> None:
        msg = EmailMessage()
        msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        msg["Message-ID"] = "<guest-fulfilled@example.test>"
        msg["Subject"] = "answer me"
        msg.set_content("substantive request\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = install_guest_manager(root)
            args = watcher.replace(guest_args(root), inbox_identity="agent@example.test\0uidvalidity=123\0guest-hees")
            args.mail_dir.mkdir()
            args.state_dir.mkdir()
            first = Client(msg.as_bytes())
            with patch.object(watcher, "store_message_images", return_value=()):
                self.assertTrue(watcher.handle_unseen(first, args))
            self.assertEqual([], first.seen)
            source = f"guest_hees_manager_mail/{watcher.mail_artifact_name(args, '41')}"
            self.assertEqual(
                source,
                fulfill_guest_hees_reply_obligation(
                    args.state_dir,
                    "<guest-fulfilled@example.test>",
                    "<verified-reply@example.test>",
                    "a" * 64,
                    "b" * 64,
                ),
            )
            replay = Client(msg.as_bytes())
            self.assertTrue(watcher.handle_unseen(replay, args))
        self.assertEqual(["41"], replay.seen)

    def test_non_guest_artifact_keeps_legacy_byte_shape(self) -> None:
        msg = EmailMessage()
        msg["Message-ID"] = "<primary@example.test>"
        msg["In-Reply-To"] = "<parent@example.test>"
        msg["References"] = "<parent@example.test>"
        msg["Subject"] = "Primary topic"
        msg.set_content("primary request\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = watcher.replace(
                guest_args(root),
                mail_dir=root / "manager_mail",
                guest_hees=False,
                self_email="human@example.test",
            )
            path = watcher.write_mail(args, "7", msg, "human@example.test", "Primary topic")
            payload = path.read_bytes()
        self.assertEqual(b"Subject: Primary topic\n\nprimary request\n", payload)

    def test_authenticated_guest_image_uses_promoted_store_contract(self) -> None:
        msg = EmailMessage()
        msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
        msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
        msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
        msg["Message-ID"] = "<guest-image@example.test>"
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
        msg["Message-ID"] = "<guest-invalid-image@example.test>"
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
                msg["Message-ID"] = f"<guest-{missing_header.lower()}@example.test>"
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
                msg["Message-ID"] = f"<guest-{boundary.lower().replace(' ', '-')}@example.test>"
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

    def test_message_text_uses_readable_html_fallback_and_ignores_hidden_content(self) -> None:
        html_only = EmailMessage()
        html_only.set_content(
            "<html><head><title>Hidden</title></head><body><p>Visible request</p><script>bad()</script><div>Second line</div></body></html>",
            subtype="html",
        )
        self.assertEqual("Visible request\nSecond line", watcher.message_text(html_only))

        alternative = EmailMessage()
        alternative.set_content("")
        alternative.add_alternative("<p>HTML alternative request</p>", subtype="html")
        self.assertEqual("HTML alternative request", watcher.message_text(alternative))

        attributes = EmailMessage()
        attributes.set_content(
            '<div hidden>hidden attribute</div><p aria-hidden="true">aria hidden</p>'
            '<section style="display: none">display hidden</section>'
            '<aside style="visibility:hidden !important">visibility hidden</aside><p>Visible answer</p>',
            subtype="html",
        )
        self.assertEqual("Visible answer", watcher.message_text(attributes))

    def test_message_text_excludes_text_attachments_and_blank_mail(self) -> None:
        attachment_only = EmailMessage()
        attachment_only.add_attachment(b"attachment request", maintype="text", subtype="plain", filename="request.txt")
        self.assertEqual("", watcher.message_text(attachment_only))

        blank = EmailMessage()
        blank.set_content("  \n")
        self.assertEqual("", watcher.message_text(blank))

    def test_blank_and_attachment_only_ingress_remain_unread_without_artifacts(self) -> None:
        blank = EmailMessage()
        blank.set_content("  \n")
        attachment_only = EmailMessage()
        attachment_only.add_attachment(b"hidden request", maintype="text", subtype="plain", filename="request.txt")
        hidden_only = EmailMessage()
        hidden_only.set_content(
            '<div hidden>hidden</div><p aria-hidden="true">also hidden</p><section style="display:none">still hidden</section>',
            subtype="html",
        )
        for name, msg in (("blank", blank), ("attachment", attachment_only), ("hidden", hidden_only)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                msg["From"] = f"Human <{GUEST_HEES_ADDRESS}>"
                msg["Return-Path"] = f"<{GUEST_HEES_ADDRESS}>"
                msg["Authentication-Results"] = f"mx.google.com; spf=pass smtp.mailfrom={GUEST_HEES_ADDRESS}"
                msg["Message-ID"] = f"<guest-{name}@example.test>"
                msg["Subject"] = name
                root = Path(tmp)
                task = install_guest_manager(root)
                args = guest_args(root)
                args.mail_dir.mkdir()
                args.state_dir.mkdir()
                client = Client(msg.as_bytes())
                with patch.object(watcher, "store_message_images", return_value=()):
                    self.assertFalse(watcher.handle_unseen(client, args))
                self.assertEqual([], client.seen)
                self.assertEqual([], list(args.mail_dir.iterdir()))
                self.assertNotIn("guest_hees_manager_mail", task.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
