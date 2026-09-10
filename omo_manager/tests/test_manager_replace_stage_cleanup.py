from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_manager_replace_stage_cleanup as cleanup


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StageCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "root"
        self.private = self.base / "private"
        self.root.mkdir()
        self.private.mkdir(mode=0o700)
        (self.root / ".git").mkdir()
        self.before = b"status: running\nmanagerat: old\n"
        self.after = b"status: done\nmanagerat: new\n"
        (self.root / "task.md").write_bytes(self.after)
        (self.root / "new.md").write_bytes(b"new\n")
        self.stage = self.root / ".task.md.omo-manager-replace-stage-0123456789abcdef0123456789abcdef"
        self.stage.write_bytes(self.before)
        record = {
            "version": "v1.0.0",
            "operation": "manager-replace",
            "state": "committed",
            "root": str(self.root),
            "completed_writes": ["task.md", "new.md"],
            "files": [
                {
                    "task": "task.md",
                    "before": base64.b64encode(self.before).decode(),
                    "after": base64.b64encode(self.after).decode(),
                    "mode": 0o644,
                    "gid": os.getgid(),
                },
                {
                    "task": "new.md",
                    "before": None,
                    "after": base64.b64encode(b"new\n").decode(),
                    "mode": 0o644,
                    "gid": os.getgid(),
                },
            ],
        }
        record["record_sha256"] = sha(json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
        self.audit = self.private / "replacement.json"
        self.audit.write_bytes(cleanup.canonical(record))
        self.audit.chmod(0o600)

    def prepare(self) -> tuple[Path, str, Path]:
        packet = self.private / "packet.json"
        final_audit = self.private / "cleanup.json"
        rc = cleanup.main(
            [
                "--prepare",
                "--audit",
                str(self.audit),
                "--audit-sha256",
                sha(self.audit.read_bytes()),
                "--output",
                str(packet),
                "--audit-output",
                str(final_audit),
            ]
        )
        self.assertEqual(0, rc)
        return packet, sha(packet.read_bytes()), final_audit

    def test_prepare_review_execute_removes_only_bound_receipt(self) -> None:
        unrelated = self.root / "keep.txt"
        unrelated.write_text("keep")
        packet, packet_sha, final_audit = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--execute",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-report",
                    str(review),
                    "--review-report-sha256",
                    sha(review.read_bytes()),
                ]
            ),
        )
        self.assertFalse(self.stage.exists())
        self.assertEqual("keep", unrelated.read_text())
        self.assertEqual("complete", json.loads(final_audit.read_text())["state"])

    def test_prepare_rejects_unmatched_stage(self) -> None:
        self.stage.write_text("foreign")
        packet = self.private / "packet.json"
        rc = cleanup.main(
            [
                "--prepare",
                "--audit",
                str(self.audit),
                "--audit-sha256",
                sha(self.audit.read_bytes()),
                "--output",
                str(packet),
                "--audit-output",
                str(self.private / "cleanup.json"),
            ]
        )
        self.assertEqual(2, rc)
        self.assertTrue(self.stage.exists())

    def test_prepare_ignores_unrelated_replacement_receipt(self) -> None:
        unrelated = self.root / ".other.md.omo-manager-replace-stage-fedcba9876543210fedcba9876543210"
        unrelated.write_text("other")
        packet, packet_sha, _audit = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--execute",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-report",
                    str(review),
                    "--review-report-sha256",
                    sha(review.read_bytes()),
                ]
            ),
        )
        self.assertTrue(unrelated.exists())

    def test_execute_rejects_new_receipt_for_selected_task(self) -> None:
        packet, packet_sha, _ = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        added = self.root / ".task.md.omo-manager-replace-stage-fedcba9876543210fedcba9876543210"
        added.write_bytes(self.before)
        rc = cleanup.main(
            [
                "--execute",
                "--packet",
                str(packet),
                "--packet-sha256",
                packet_sha,
                "--review-report",
                str(review),
                "--review-report-sha256",
                sha(review.read_bytes()),
            ]
        )
        self.assertEqual(2, rc)
        self.assertTrue(self.stage.exists())
        self.assertTrue(added.exists())

    def test_execute_rejects_receipt_drift(self) -> None:
        packet, packet_sha, _ = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        self.stage.write_text("drift")
        rc = cleanup.main(
            [
                "--execute",
                "--packet",
                str(packet),
                "--packet-sha256",
                packet_sha,
                "--review-report",
                str(review),
                "--review-report-sha256",
                sha(review.read_bytes()),
            ]
        )
        self.assertEqual(2, rc)
        self.assertTrue(self.stage.exists())

    def test_execute_rejects_bound_receipt_absent_without_custody(self) -> None:
        packet, packet_sha, final_audit = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        self.stage.unlink()
        self.assertEqual(
            2,
            cleanup.main(
                [
                    "--execute",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-report",
                    str(review),
                    "--review-report-sha256",
                    sha(review.read_bytes()),
                ]
            ),
        )
        self.assertFalse(final_audit.exists())

    def test_execute_recovers_custody_move_after_interruption(self) -> None:
        packet, packet_sha, final_audit = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        real_move = cleanup.move_noreplace

        def interrupted(source_parent_fd: int, source: str, destination_parent_fd: int, destination: str) -> None:
            real_move(source_parent_fd, source, destination_parent_fd, destination)
            raise OSError("injected interruption")

        invocation = [
            "--execute",
            "--packet",
            str(packet),
            "--packet-sha256",
            packet_sha,
            "--review-report",
            str(review),
            "--review-report-sha256",
            sha(review.read_bytes()),
        ]
        with patch.object(cleanup, "move_noreplace", side_effect=interrupted):
            self.assertEqual(2, cleanup.main(invocation))
        custody = list((self.root / ".git" / "omo-manager-replace-cleanup" / packet_sha).iterdir())
        self.assertEqual(1, len(custody))
        self.assertEqual(0, cleanup.main(invocation))
        self.assertTrue(custody[0].exists())
        self.assertEqual("complete", json.loads(final_audit.read_text())["state"])

    def test_execute_supports_setgid_git_directory(self) -> None:
        (self.root / ".git").chmod(0o2775)
        packet, packet_sha, final_audit = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--execute",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-report",
                    str(review),
                    "--review-report-sha256",
                    sha(review.read_bytes()),
                ]
            ),
        )
        custody = Path(json.loads(final_audit.read_text())["custody_directory"])
        self.assertEqual(0o700, custody.stat().st_mode & 0o7777)

    def test_execute_rejects_post_move_custody_drift(self) -> None:
        packet, packet_sha, final_audit = self.prepare()
        review = self.private / "review.json"
        self.assertEqual(
            0,
            cleanup.main(
                [
                    "--review",
                    "--packet",
                    str(packet),
                    "--packet-sha256",
                    packet_sha,
                    "--review-output",
                    str(review),
                ]
            ),
        )
        real_validate = cleanup.validate_custody_file

        def drift(parent_fd: int, name: str, raw: dict[str, object]) -> None:
            descriptor = os.open(name, os.O_WRONLY, dir_fd=parent_fd)
            try:
                os.ftruncate(descriptor, 0)
                os.write(descriptor, b"drift")
            finally:
                os.close(descriptor)
            real_validate(parent_fd, name, raw)

        with patch.object(cleanup, "validate_custody_file", side_effect=drift):
            self.assertEqual(
                2,
                cleanup.main(
                    [
                        "--execute",
                        "--packet",
                        str(packet),
                        "--packet-sha256",
                        packet_sha,
                        "--review-report",
                        str(review),
                        "--review-report-sha256",
                        sha(review.read_bytes()),
                    ]
                ),
            )
        self.assertFalse(final_audit.exists())


if __name__ == "__main__":
    unittest.main()
