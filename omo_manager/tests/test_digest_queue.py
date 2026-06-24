from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from omo_manager.omo_digest_queue import decide_delivery, parse_items, queue_path
from omo_manager.omo_pending_watch import find_markers


class DigestQueueTests(unittest.TestCase):
    def test_submit_initializes_header_with_a_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "submit",
                    "--source",
                    "pb-news-watch",
                    "--title",
                    "Queued item",
                    "--summary",
                    "Non-urgent.",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            text = (root / "MANAGER_DIGEST_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("may email the human directly with `[a]`", text)

    def test_submit_dedupes_and_pending_watcher_ignores_queue_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cmd = [
                str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                "--root",
                str(root),
                "submit",
                "--source",
                "pb-news-watch",
                "--title",
                "Old but useful item",
                "--url",
                "https://example.test/item",
                "--age",
                "3 days old",
                "--summary",
                "Useful context, not breaking news.",
            ]
            first = subprocess.run(cmd, text=True, capture_output=True, timeout=10, check=False)
            second = subprocess.run(cmd, text=True, capture_output=True, timeout=10, check=False)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            path = root / "MANAGER_DIGEST_QUEUE.md"
            text = path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("\n(digest-item)\n"))
            self.assertEqual([], find_markers(root, [path]))

    def test_delivery_gate_blocks_before_noon_and_recent_inbound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = queue_path(root, Path("MANAGER_DIGEST_QUEUE.md"))
            _ = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "submit",
                    "--source",
                    "pb-news-watch",
                    "--title",
                    "Queued item",
                    "--summary",
                    "Non-urgent.",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            recent = datetime(2026, 5, 25, 13, 30).astimezone()
            mail = mail_dir / "4057.txt"
            mail.write_text("Date: Mon, 25 May 2026 13:30:00 -0700\n\nbody\n", encoding="utf-8")
            items = parse_items(path.read_text(encoding="utf-8"))
            before_noon = decide_delivery(mail_dir, state, items, datetime(2026, 5, 25, 11, 0).astimezone(), 90, 120, 240)
            self.assertFalse(before_noon.eligible)
            self.assertIn("before-noon", before_noon.reasons)
            active = decide_delivery(mail_dir, state, items, recent + timedelta(minutes=20), 90, 120, 240)
            self.assertFalse(active.eligible)
            self.assertTrue(any(reason.startswith("recent-human-inbound") for reason in active.reasons))

    def test_dry_run_delivery_renders_without_sending_or_marking_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            submit = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "submit",
                    "--source",
                    "pb-news-watch",
                    "--title",
                    "Queued item",
                    "--url",
                    "https://example.test",
                    "--published-at",
                    "2026-05-25T14:00:00-07:00",
                    "--summary",
                    "Non-urgent.",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, submit.returncode, submit.stderr)
            delivery = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "deliver-once",
                    "--state-dir",
                    str(state),
                    "--dry-run",
                    "--now",
                    "2026-05-25T15:00:00-07:00",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, delivery.returncode, delivery.stderr)
            self.assertIn("[Queued item](https://example.test)", delivery.stdout)
            self.assertIn("Published: 2026-05-25 14:00", delivery.stdout)
            self.assertIn("Queued:", delivery.stdout)
            text = (root / "MANAGER_DIGEST_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("status: queued", text)
            self.assertNotIn("status: sent", text)

    def test_legacy_relative_age_renders_as_absolute_published_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "MANAGER_DIGEST_QUEUE.md"
            path.write_text(
                "# Manager non-urgent digest queue\n\n"
                "---\n(digest-item)\n"
                "id: old\nstatus: queued\nqueued-at: 2026-05-25T20:00:00-07:00\n"
                "source: pb\ntitle: Old item\nurl: https://example.test/old\nage: Published 3 hours ago\nsummary:\n> Old summary.\n",
                encoding="utf-8",
            )
            delivery = subprocess.run(
                [str(Path.home() / ".config/omo_manager/omo_digest_queue.py"), "--root", str(root), "deliver-once", "--dry-run"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, delivery.returncode, delivery.stderr)
            self.assertIn("Published: 2026-05-25 17:00", delivery.stdout)
            self.assertNotIn("Published 3 hours ago", delivery.stdout)

    def test_cli_delivery_ignores_idle_gate_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submit = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "submit",
                    "--source",
                    "pb-news-watch",
                    "--title",
                    "Queued item",
                    "--summary",
                    "Non-urgent.",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, submit.returncode, submit.stderr)
            mail_dir = root / "manager_mail"
            mail_dir.mkdir()
            (mail_dir / "4057.txt").write_text("Date: Mon, 25 May 2026 14:30:00 -0700\n\nbody\n", encoding="utf-8")
            delivery = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "deliver-once",
                    "--mail-dir",
                    str(mail_dir),
                    "--min-human-inbound-idle-min",
                    "90",
                    "--dry-run",
                    "--now",
                    "2026-05-25T15:00:00-07:00",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, delivery.returncode, delivery.stderr)
            self.assertIn("Queued item", delivery.stdout)
            self.assertNotIn("recent-human-inbound", delivery.stdout)

    def test_rejects_unsafe_id_and_nonpositive_max_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_id = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "submit",
                    "--source",
                    "pb-news-watch",
                    "--title",
                    "Queued item",
                    "--summary",
                    "Non-urgent.",
                    "--id",
                    "safe\n(pending)",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, bad_id.returncode)
            self.assertIn("id must match", bad_id.stderr)
            bad_max = subprocess.run(
                [
                    str(Path.home() / ".config/omo_manager/omo_digest_queue.py"),
                    "--root",
                    str(root),
                    "deliver-once",
                    "--dry-run",
                    "--max-items",
                    "0",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, bad_max.returncode)
            self.assertIn("--max-items must be >= 1", bad_max.stderr)

    def test_actual_send_marks_item_sent_with_fake_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            fake = root / "fake-send.sh"
            fake.write_text(
                f"#!/usr/bin/env bash\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    --subject-file) subject_file=\"$2\"; shift 2 ;;\n"
                "    --message-file) message_file=\"$2\"; shift 2 ;;\n"
                "    *) echo \"bad arg: $1\" >&2; exit 2 ;;\n"
                "  esac\n"
                "done\n"
                f"cat \"$subject_file\" \"$message_file\" > {str(root / 'fake-send.log')!r}\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            submit = subprocess.run(
                [str(Path.home() / ".config/omo_manager/omo_digest_queue.py"), "--root", str(root), "submit", "--source", "pb", "--title", "Queued item", "--summary", "Non-urgent."],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, submit.returncode, submit.stderr)
            from omo_manager import omo_digest_queue
            from unittest.mock import patch
            args = omo_digest_queue.build_parser().parse_args([
                "--root", str(root), "deliver-once", "--state-dir", str(state), "--send-helper", str(fake), "--min-manager-outbound-idle-min", "0", "--min-human-inbound-idle-min", "0", "--min-delivery-gap-min", "0"
            ])
            args.root = args.root.resolve()
            with patch.object(omo_digest_queue, "now_local", return_value=datetime.fromisoformat("2026-05-25T15:00:00-07:00")):
                self.assertEqual(0, omo_digest_queue.command_deliver(args))
            sent_text = (root / "fake-send.log").read_text(encoding="utf-8")
            self.assertIn("[a] Non-urgent news digest", sent_text)
            self.assertIn("Queued item", sent_text)
            queue_text = (root / "MANAGER_DIGEST_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("status: sent", queue_text)
            self.assertIn("sent-at:", queue_text)
            self.assertNotIn("status: queued", queue_text)

    def test_omo_email_human_post_send_log_failure_still_exits_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            helper_dir = home / ".config" / "helper.sh"
            helper_dir.mkdir(parents=True)
            email_helper = helper_dir / "email_me.py"
            email_helper.write_text(f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(Path(tmp) / 'sent.txt')!r}).write_text('sent')\n", encoding="utf-8")
            email_helper.chmod(0o755)
            msg = Path(tmp) / "msg.md"
            msg.write_text("body\n", encoding="utf-8")
            subject = Path(tmp) / "subject.txt"
            subject.write_text("[omo_manager] test\n", encoding="utf-8")
            bad_state = Path(tmp) / "not-a-dir"
            bad_state.write_text("file blocks mkdir\n", encoding="utf-8")
            result = subprocess.run(
                [str(Path.home() / ".config/omo_manager/omo_email_human.sh"), "--subject-file", str(subject), "--message-file", str(msg)],
                cwd=tmp,
                env={"HOME": str(home), "OMO_MANAGER_STATE_DIR": str(bad_state), "PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("Emailed the human\n", result.stdout)
            self.assertEqual("sent", (Path(tmp) / "sent.txt").read_text(encoding="utf-8"))

    def test_first_use_concurrent_submit_and_deliver_preserves_item(self) -> None:
        for _ in range(10):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state"
                submit_cmd = [str(Path.home() / ".config/omo_manager/omo_digest_queue.py"), "--root", str(root), "submit", "--source", "pb", "--title", "Race item", "--summary", "Non-urgent."]
                deliver_cmd = [str(Path.home() / ".config/omo_manager/omo_digest_queue.py"), "--root", str(root), "deliver-once", "--state-dir", str(state), "--dry-run", "--now", "2026-05-25T15:00:00-07:00"]
                submit = subprocess.Popen(submit_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                deliver = subprocess.Popen(deliver_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                submit_out, submit_err = submit.communicate(timeout=10)
                deliver_out, deliver_err = deliver.communicate(timeout=10)
                self.assertEqual(0, submit.returncode, submit_err + submit_out)
                self.assertEqual(0, deliver.returncode, deliver_err + deliver_out)
                queue_text = (root / "MANAGER_DIGEST_QUEUE.md").read_text(encoding="utf-8")
                self.assertIn("title: Race item", queue_text)
                self.assertEqual(1, queue_text.count("\n(digest-item)\n"))

    def test_after_23_gate_and_prior_digest_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            path = queue_path(root, Path("MANAGER_DIGEST_QUEUE.md"))
            subprocess.run([str(Path.home() / ".config/omo_manager/omo_digest_queue.py"), "--root", str(root), "submit", "--source", "pb", "--title", "Queued item", "--summary", "Non-urgent."], check=False, timeout=10)
            items = parse_items(path.read_text(encoding="utf-8"))
            late = decide_delivery(root / "manager_mail", state, items, datetime.fromisoformat("2026-05-25T23:00:00-07:00"), 90, 120, 240)
            self.assertFalse(late.eligible)
            self.assertIn("after-evening-window", late.reasons)
            sent_text = path.read_text(encoding="utf-8").replace("summary:\n> Non-urgent.\n", "summary:\n> Non-urgent.\nsent-at: 2026-05-25T14:00:00-07:00\n")
            path.write_text(sent_text, encoding="utf-8")
            gap_items = parse_items(path.read_text(encoding="utf-8"))
            gap = decide_delivery(root / "manager_mail", state, gap_items, datetime.fromisoformat("2026-05-25T15:00:00-07:00"), 90, 120, 240)
            self.assertFalse(gap.eligible)
            self.assertTrue(any(reason.startswith("recent-digest-delivery") for reason in gap.reasons))


if __name__ == "__main__":
    unittest.main()
