from __future__ import annotations

import hashlib
import json
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_guest_images as images

GUEST_HEES_ADDRESS = images.GUEST_HEES_ADDRESS
GUEST_HEES_MANAGER_TARGET = "guest_hees:7"

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 13 + b"\x00\x00\x00\x00IEND\x00\x00\x00\x00"
JPEG = b"\xff\xd8\xff\xe0data\xff\xd9"
GIF = b"GIF89a" + b"\x00" * 7 + b";"
WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8X" + b"\x00" * 4


def message_with_image(
    payload: bytes = PNG,
    *,
    filename: str = "guest.png",
    maintype: str = "image",
    subtype: str = "png",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = GUEST_HEES_ADDRESS
    message.set_content("request")
    message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return message


def store(message: EmailMessage, root: Path, *, source_id: str = "gmail:uidvalidity:42") -> tuple[str, ...]:
    return images.store_message_images(
        message,
        sender=GUEST_HEES_ADDRESS,
        route_target=GUEST_HEES_MANAGER_TARGET,
        authentication=images.AUTHENTICATION,
        source_id=source_id,
        root=root,
        received_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


class GuestImageTests(unittest.TestCase):
    def test_all_supported_types_require_matching_declared_mime_and_extension(self) -> None:
        cases = (
            (PNG, "image.png", "png"),
            (JPEG, "image.jpeg", "jpeg"),
            (GIF, "image.gif", "gif"),
            (WEBP, "image.webp", "webp"),
        )
        for number, (payload, filename, subtype) in enumerate(cases):
            with self.subTest(subtype=subtype), tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
                root = Path(tmp) / "runtime"
                reference = store(message_with_image(payload, filename=filename, subtype=subtype), root, source_id=f"uid:{number}")[0]
                self.assertEqual(f"image/{'jpeg' if subtype == 'jpeg' else subtype}", images.resolve_reference(reference, root).mime_type)

    def test_store_creates_private_receipt_and_service_resolution(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            digest = hashlib.sha256(PNG).hexdigest()
            self.assertEqual(f"guest-image:v1:{digest}", reference)
            resolution = images.resolve_for_service(reference, service="chatgpt", root=root)
            receipt = json.loads(resolution.batch_receipt.read_text(encoding="utf-8"))

            self.assertEqual("omo-guest-image-batch/v1", receipt["schema"])
            self.assertEqual("active", receipt["state"])
            self.assertEqual(GUEST_HEES_ADDRESS, receipt["sender"])
            self.assertEqual(GUEST_HEES_MANAGER_TARGET, receipt["route_target"])
            self.assertEqual(images.AUTHENTICATION, receipt["authentication"])
            self.assertEqual(reference, receipt["images"][0]["reference"])
            self.assertEqual("omo-guest-image-resolution/v1", resolution.schema)
            self.assertEqual("chatgpt", resolution.service)
            self.assertEqual(digest, resolution.sha256)
            self.assertEqual("image/png", resolution.mime_type)
            self.assertEqual(hashlib.sha256(resolution.batch_receipt.read_bytes()).hexdigest(), resolution.batch_receipt_sha256)
            self.assertEqual(0o700, stat.S_IMODE(root.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(resolution.path.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(resolution.batch_receipt.stat().st_mode))

    def test_inbound_requires_exact_sender_and_route(self) -> None:
        message = message_with_image()
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            with self.assertRaisesRegex(images.GuestImageError, "exact pinned sender"):
                images.store_message_images(message, sender="other@qq.com", route_target=GUEST_HEES_MANAGER_TARGET, authentication=images.AUTHENTICATION, source_id="uid:1", root=root)
            with self.assertRaisesRegex(images.GuestImageError, "dedicated guest manager route"):
                images.store_message_images(message, sender=GUEST_HEES_ADDRESS, route_target="other:9", authentication=images.AUTHENTICATION, source_id="uid:1", root=root)
            with self.assertRaisesRegex(images.GuestImageError, "authenticated-intake marker"):
                images.store_message_images(message, sender=GUEST_HEES_ADDRESS, route_target=GUEST_HEES_MANAGER_TARGET, authentication="untrusted", source_id="uid:1", root=root)
            with self.assertRaisesRegex(images.GuestImageError, "exact pinned sender"):
                images.store_message_images(message, sender=GUEST_HEES_ADDRESS.upper(), route_target=GUEST_HEES_MANAGER_TARGET, authentication=images.AUTHENTICATION, source_id="uid:1", root=root)
            self.assertFalse(root.exists())

    def test_mime_extension_content_filename_and_count_fail_closed(self) -> None:
        invalid_messages = [
            message_with_image(filename="guest.jpg"),
            message_with_image(b"not an image"),
            message_with_image(filename="../guest.png"),
            message_with_image(maintype="application", subtype="octet-stream"),
        ]
        too_many = EmailMessage()
        too_many.set_content("request")
        for number in range(images.MAX_IMAGES_PER_MESSAGE + 1):
            too_many.add_attachment(PNG, maintype="image", subtype="png", filename=f"{number}.png")
        invalid_messages.append(too_many)
        duplicate = EmailMessage()
        duplicate.set_content("request")
        duplicate.add_attachment(PNG, maintype="image", subtype="png", filename="one.png")
        duplicate.add_attachment(PNG, maintype="image", subtype="png", filename="two.png")
        invalid_messages.append(duplicate)

        for number, message in enumerate(invalid_messages):
            with self.subTest(number=number), tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
                root = Path(tmp) / "runtime"
                with self.assertRaises(images.GuestImageError):
                    store(message, root)
                self.assertFalse(root.exists())

    def test_multipart_attachment_rejects_otherwise_valid_image_message(self) -> None:
        message = message_with_image()
        nested = EmailMessage()
        nested.set_content("forwarded content")
        message.add_attachment(nested, filename="forwarded.eml")
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            with self.assertRaisesRegex(images.GuestImageError, "multipart MIME attachments"):
                store(message, Path(tmp) / "runtime")

    def test_size_and_store_capacity_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            with patch.object(images, "MAX_IMAGE_BYTES", len(PNG) - 1):
                with self.assertRaisesRegex(images.GuestImageError, "at most"):
                    store(message_with_image(), root)
            self.assertFalse(root.exists())

        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            with patch.object(images, "MAX_STORED_IMAGES", 0):
                with self.assertRaisesRegex(images.GuestImageError, "capacity"):
                    store(message_with_image(), root)
            self.assertFalse(list((root / "objects").glob("*")))
            self.assertFalse(list((root / "batches").glob("*")))

    def test_oversized_base64_is_rejected_before_decode(self) -> None:
        message = message_with_image(PNG * 4)
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp, patch.object(images, "MAX_IMAGE_BYTES", 8), patch.object(images.base64, "b64decode", side_effect=AssertionError("must reject before decode")):
            with self.assertRaisesRegex(images.GuestImageError, "bounded decode limit"):
                store(message, Path(tmp) / "runtime")

    def test_root_rejects_git_ancestry_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            parent = Path(tmp)
            repository = parent / "repository"
            (repository / ".git").mkdir(parents=True)
            with self.assertRaisesRegex(images.GuestImageError, "outside every Git repository"):
                store(message_with_image(), repository / "runtime")

            bare = parent / "bare.git"
            (bare / "objects").mkdir(parents=True)
            (bare / "refs").mkdir()
            (bare / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            with self.assertRaisesRegex(images.GuestImageError, "bare repositories"):
                store(message_with_image(), bare / "runtime")

            private = parent / "private"
            private.mkdir(mode=0o700)
            link = parent / "link"
            link.symlink_to(private, target_is_directory=True)
            with self.assertRaisesRegex(images.GuestImageError, "symlinks"):
                store(message_with_image(), link)

    def test_outbound_requires_exact_recipient_and_revalidates_object(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            with self.assertRaisesRegex(images.GuestImageError, "exact pinned recipient"):
                images.reply_attachments((reference,), recipient="other@qq.com", root=root)
            with self.assertRaisesRegex(images.GuestImageError, "exact pinned recipient"):
                images.reply_attachments((reference,), recipient=GUEST_HEES_ADDRESS.upper(), root=root)
            with self.assertRaisesRegex(images.GuestImageError, "one to four"):
                images.reply_attachments((), recipient=GUEST_HEES_ADDRESS, root=root)
            attachment = images.reply_attachments((reference,), recipient=GUEST_HEES_ADDRESS, root=root)[0]
            self.assertEqual(PNG, attachment.data)
            with self.assertRaisesRegex(images.GuestImageError, "unique references"):
                images.reply_attachments((reference, reference), recipient=GUEST_HEES_ADDRESS, root=root)

            replacement = root / "replacement.png"
            replacement.write_bytes(PNG)
            attachment.path.unlink()
            attachment.path.symlink_to(replacement)
            with self.assertRaises(images.GuestImageError):
                images.reply_attachments((reference,), recipient=GUEST_HEES_ADDRESS, root=root)

    def test_service_resolution_rejects_unknown_service_and_tampering(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            with self.assertRaisesRegex(images.GuestImageError, "unsupported guest image service"):
                images.resolve_for_service(reference, service="other", root=root)
            image = images.resolve_reference(reference, root)
            image.path.write_bytes(PNG + b"tampered")
            with self.assertRaisesRegex(images.GuestImageError, "digest"):
                images.resolve_for_service(reference, service="gemini", root=root)

    def test_receipt_trust_field_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            batch = next((root / "batches").glob("*.json"))
            metadata = json.loads(batch.read_text(encoding="utf-8"))
            metadata["authentication"] = "forged"
            batch.write_text(json.dumps(metadata), encoding="utf-8")
            batch.chmod(0o600)
            with self.assertRaisesRegex(images.GuestImageError, "trust fields"):
                images.resolve_for_service(reference, service="chatgpt", root=root)

    def test_receipt_source_id_must_match_hashed_batch_filename(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            batch = next((root / "batches").glob("*.json"))
            metadata = json.loads(batch.read_text(encoding="utf-8"))
            metadata["source_id"] = "different-source-id"
            batch.write_text(json.dumps(metadata), encoding="utf-8")
            batch.chmod(0o600)
            with self.assertRaisesRegex(images.GuestImageError, "source identity"):
                images.resolve_reference(reference, root)
            with self.assertRaisesRegex(images.GuestImageError, "source identity"):
                images.resolve_for_service(reference, service="chatgpt", root=root)

    def test_active_receipt_rejects_noncanonical_timestamp(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            batch = next((root / "batches").glob("*.json"))
            metadata = json.loads(batch.read_text(encoding="utf-8"))
            metadata["received_at"] = "not-a-timestamp"
            batch.write_text(json.dumps(metadata), encoding="utf-8")
            batch.chmod(0o600)
            with self.assertRaisesRegex(images.GuestImageError, "canonical UTC timestamp"):
                images.resolve_for_service(reference, service="chatgpt", root=root)

    def test_quarantine_receipt_rejects_invalid_scalar_fields(self) -> None:
        for variant in ("boolean_size", "boolean_days", "bad_timestamp"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
                root = Path(tmp) / "runtime"
                reference = store(message_with_image(), root)[0]
                receipt_path = images.quarantine_expired(older_than_days=7, root=root, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
                assert receipt_path is not None
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if variant == "boolean_size":
                    receipt["entries"][0]["size_bytes"] = True
                elif variant == "boolean_days":
                    receipt["older_than_days"] = True
                else:
                    receipt["created_at"] = "yesterday"
                receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                receipt_path.chmod(0o600)
                with self.assertRaises(images.GuestImageError):
                    images.resolve_reference(reference, root)

    def test_receipt_rejects_zero_five_and_malformed_unrequested_entries(self) -> None:
        for variant in ("zero", "five", "malformed", "boolean_size"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
                root = Path(tmp) / "runtime"
                reference = store(message_with_image(), root)[0]
                batch = next((root / "batches").glob("*.json"))
                metadata = json.loads(batch.read_text(encoding="utf-8"))
                original = metadata["images"][0]
                if variant == "zero":
                    metadata["images"] = []
                else:
                    additions = []
                    for number in range(1, 5 if variant == "five" else 2):
                        digest = f"{number:064x}"
                        additions.append(
                            {
                                "filename": f"{number}.png",
                                "mime_type": "image/png",
                                "size_bytes": 1,
                                "sha256": digest,
                                "reference": f"guest-image:v1:{digest}",
                                "object": f"objects/{digest}.png",
                            }
                        )
                    if variant == "malformed":
                        additions[0]["object"] = "../../escape.png"
                    if variant == "boolean_size":
                        additions[0]["size_bytes"] = True
                    metadata["images"] = [original, *additions]
                batch.write_text(json.dumps(metadata), encoding="utf-8")
                batch.chmod(0o600)
                with self.assertRaises(images.GuestImageError):
                    images.resolve_for_service(reference, service="chatgpt", root=root)

    def test_source_is_idempotent_but_conflicting_replay_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            first = store(message_with_image(), root)
            self.assertEqual(first, store(message_with_image(), root))
            other = message_with_image(filename="different.png")
            with self.assertRaisesRegex(images.GuestImageError, "does not match"):
                store(other, root)

    def test_interrupted_store_resumes_from_durable_planned_batch(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            original_publish = images._publish_new_file
            interrupted = False

            def interrupt_after_object(path: Path, payload: bytes) -> None:
                nonlocal interrupted
                original_publish(path, payload)
                if path.parent.name == "objects" and not interrupted:
                    interrupted = True
                    raise SystemExit("forced interruption")

            with patch.object(images, "_publish_new_file", side_effect=interrupt_after_object):
                with self.assertRaises(SystemExit):
                    store(message_with_image(), root)
            batch = next((root / "batches").glob("*.json"))
            self.assertEqual("planned", json.loads(batch.read_text(encoding="utf-8"))["state"])

            reference = store(message_with_image(), root)[0]
            self.assertEqual(PNG, images.resolve_reference(reference, root).data)
            self.assertEqual("active", json.loads(batch.read_text(encoding="utf-8"))["state"])

    def test_interrupted_new_root_directory_sync_retries_identically(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            original_fsync = images._fsync_dir
            interrupted = False

            def interrupt_after_objects_sync(path: Path) -> None:
                nonlocal interrupted
                original_fsync(path)
                if path.name == "objects" and not interrupted:
                    interrupted = True
                    raise SystemExit("forced directory interruption")

            with patch.object(images, "_fsync_dir", side_effect=interrupt_after_objects_sync):
                with self.assertRaises(SystemExit):
                    store(message_with_image(), root)
            self.assertTrue((root / "objects").is_dir())

            reference = store(message_with_image(), root)[0]
            self.assertEqual(PNG, images.resolve_reference(reference, root).data)

    def test_interrupted_quarantine_rolls_forward_on_next_operation(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            original_move = images._durable_move
            interrupted = False

            def interrupt_after_move(source: Path, destination: Path) -> None:
                nonlocal interrupted
                original_move(source, destination)
                if not interrupted:
                    interrupted = True
                    raise SystemExit("forced interruption")

            with patch.object(images, "_durable_move", side_effect=interrupt_after_move):
                with self.assertRaises(SystemExit):
                    images.quarantine_expired(older_than_days=7, root=root, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
            receipt = next((root / "quarantine").glob("*/receipt.json"))
            self.assertEqual("planned", json.loads(receipt.read_text(encoding="utf-8"))["state"])

            with self.assertRaises(images.GuestImageError):
                images.resolve_reference(reference, root)
            self.assertEqual("quarantined", json.loads(receipt.read_text(encoding="utf-8"))["state"])

    def test_interrupted_quarantine_directory_sync_retries_safely(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            original_fsync = images._fsync_dir
            interrupted = False

            def interrupt_after_quarantine_objects_sync(path: Path) -> None:
                nonlocal interrupted
                original_fsync(path)
                if path.name == "objects" and path.parent.parent.name == "quarantine" and not interrupted:
                    interrupted = True
                    raise SystemExit("forced quarantine directory interruption")

            with patch.object(images, "_fsync_dir", side_effect=interrupt_after_quarantine_objects_sync):
                with self.assertRaises(SystemExit):
                    images.quarantine_expired(older_than_days=7, root=root, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
            self.assertEqual(PNG, images.resolve_reference(reference, root).data)

            receipt = images.quarantine_expired(older_than_days=7, root=root, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
            self.assertIsNotNone(receipt)
            with self.assertRaises(images.GuestImageError):
                images.resolve_reference(reference, root)

    def test_interrupted_restore_resumes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            receipt = images.quarantine_expired(older_than_days=7, root=root, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
            assert receipt is not None
            original_move = images._durable_move
            interrupted = False

            def interrupt_after_move(source: Path, destination: Path) -> None:
                nonlocal interrupted
                original_move(source, destination)
                if not interrupted:
                    interrupted = True
                    raise SystemExit("forced interruption")

            with patch.object(images, "_durable_move", side_effect=interrupt_after_move):
                with self.assertRaises(SystemExit):
                    images.restore_quarantine(receipt, root=root)
            self.assertEqual("restoring", json.loads(receipt.read_text(encoding="utf-8"))["state"])

            images.restore_quarantine(receipt, root=root)
            self.assertEqual(PNG, images.resolve_reference(reference, root).data)
            self.assertEqual("restored", json.loads(receipt.read_text(encoding="utf-8"))["state"])

    def test_explicit_cleanup_quarantines_and_exact_restore_recovers(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            reference = store(message_with_image(), root)[0]
            now = datetime(2026, 8, 28, tzinfo=timezone.utc)
            receipt_path = images.quarantine_expired(older_than_days=7, root=root, now=now)
            self.assertIsNotNone(receipt_path)
            assert receipt_path is not None
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual("omo-guest-image-quarantine/v1", receipt["schema"])
            self.assertEqual("quarantined", receipt["state"])
            self.assertTrue(receipt["entries"])
            with self.assertRaises(images.GuestImageError):
                images.resolve_reference(reference, root)

            images.restore_quarantine(receipt_path, root=root, now=now + timedelta(minutes=1))
            self.assertEqual(PNG, images.resolve_reference(reference, root).data)
            restored = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual("restored", restored["state"])
            images.restore_quarantine(receipt_path, root=root)

    def test_restore_rejects_receipt_path_escape(self) -> None:
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            _ = store(message_with_image(), root)
            receipt_path = images.quarantine_expired(older_than_days=7, root=root, now=datetime(2026, 8, 28, tzinfo=timezone.utc))
            assert receipt_path is not None
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["entries"][0]["from"] = "../../escape"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            receipt_path.chmod(0o600)
            with self.assertRaisesRegex(images.GuestImageError, "unsafe move entry"):
                images.restore_quarantine(receipt_path, root=root)

    def test_empty_message_creates_no_runtime_root(self) -> None:
        message = EmailMessage()
        message.set_content("text only")
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            root = Path(tmp) / "runtime"
            self.assertEqual((), store(message, root))
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
