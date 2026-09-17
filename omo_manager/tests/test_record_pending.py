from __future__ import annotations

import io
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_blocking import ENABLE_FILE
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_record_pending import Args
from omo_manager.omo_record_pending import ack_sent_line
from omo_manager.omo_record_pending import parse_args
from omo_manager.omo_record_pending import pending_notice_key
from omo_manager.omo_record_pending import recorded_line
from omo_manager.omo_record_pending import run


def task_frontmatter(*, status: str = "running", is_manager: bool = False, pending_items: tuple[str, ...] = ()) -> str:
    lines = [
        "---",
        "version: v1.0.0",
        f"status: {status}",
        "runat: wl:2",
        "tool: codex",
        "managerat: wl:1",
        f"is_manager: {str(is_manager).lower()}",
    ]
    if pending_items:
        lines.append("pending_task_items:")
        lines.extend(f"  - {item}" for item in pending_items)
    else:
        lines.append("pending_task_items: []")
    lines.append("---")
    return "\n".join(lines) + "\n"


def v2_task_frontmatter() -> str:
    return """---
version: v2.0.0
task_id: task_019f0000-0000-7000-8000-000000000041
status: running
runat: wl:2
tool: codex
managerat: wl:1
is_manager: false
pending_task_items: []
resolved_task_items: []
---
"""


def human_email(root: Path, name: str = "source.txt", subject: str = "Fresh Human request") -> Path:
    relative = Path("manager_mail") / name
    path = root / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"Subject: {subject}\n\nPlease do it.\n", encoding="utf-8")
    return relative


class RecordPendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_tmp = tempfile.TemporaryDirectory()
        self.state_env = patch.dict("os.environ", {"OMO_MANAGER_STATE_DIR": self.state_tmp.name})
        self.state_env.start()

    def tearDown(self) -> None:
        self.state_env.stop()
        self.state_tmp.cleanup()

    def test_pending_notice_identity_canonicalizes_equivalent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter(), encoding="utf-8")
            plain = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 review",), True, human_email(root))
            equivalent = Args(root, Path("./task.md"), 10, task, ("🧑 review",), True, human_email(root))
            self.assertEqual(pending_notice_key(plain), pending_notice_key(equivalent))

    def test_insert_requires_and_encodes_explicit_provenance(self) -> None:
        base = ["--pending-file", "task.md", "--line", "10", "--item", "review request"]
        with self.assertRaises(SystemExit):
            parse_args(base)
        with self.assertRaises(SystemExit):
            parse_args([*base, "--human-authored"])
        self.assertEqual(
            ("🧑 review request",),
            parse_args([*base, "--human-authored", "--ack-human", "--email-file", "manager_mail/source.txt"]).items,
        )
        self.assertEqual(("review request",), parse_args([*base, "--agent-authored"]).items)
        with self.assertRaises(SystemExit):
            parse_args([*base, "--agent-authored", "--ack-human"])
        for old_flag in ("--human", "--agent"):
            with self.subTest(old_flag=old_flag), self.assertRaises(SystemExit):
                parse_args([*base, old_flag])

    def test_human_item_without_ack_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter() + "(pending)\nPlease do it.\n"
            task.write_text(original, encoding="utf-8")

            self.assertEqual(2, run(Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), False)))

            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_unmarked_item_with_ack_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter() + "(pending)\nPlease do it.\n"
            task.write_text(original, encoding="utf-8")

            with patch("omo_manager.omo_record_pending.subprocess.run") as send:
                self.assertEqual(2, run(Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), True, human_email(root))))

            send.assert_not_called()
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_records_human_item_as_v2_object_after_enablement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(v2_task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            (root / ENABLE_FILE).write_text("version: v2.0.0\nenabled: true\n", encoding="utf-8")
            line = task.read_text(encoding="utf-8").splitlines().index("(pending)") + 1

            exit_code = run(Args(root, Path("task.md"), line, Path("task.md"), ("finish review",), False))

            text = task.read_text(encoding="utf-8")
            metadata = parse_task_metadata(text, root)
            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", text)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(("finish review",), metadata.pending_task_items)
            self.assertTrue(metadata.pending_items[0].id.startswith("pi_"))

    def test_rejects_v2_recording_before_enablement_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = v2_task_frontmatter() + "(pending)\nPlease do it.\n"
            task.write_text(original, encoding="utf-8")
            line = original.splitlines().index("(pending)") + 1

            exit_code = run(Args(root, Path("task.md"), line, Path("task.md"), ("finish review",), False))

            self.assertEqual(2, exit_code)
            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_records_items_and_removes_pending_line_in_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("finish review", "email result"), False))

            text = task.read_text(encoding="utf-8")
            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", text)
            self.assertIn("pending_task_items:\n  - finish review\n  - email result\n", text)
            self.assertIn("Please do it.\n", text)

    def test_records_mapping_prone_item_as_quoted_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            item = "fresh residual review: exact bookkeeping candidate"

            self.assertEqual(0, run(Args(root, Path("task.md"), 10, Path("task.md"), (item,), False)))

            text = task.read_text(encoding="utf-8")
            self.assertIn("pending_task_items:\n  - 'fresh residual review: exact bookkeeping candidate'\n", text)
            metadata = parse_task_metadata(text, root)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual((item,), metadata.pending_task_items)

    def test_records_yaml_control_and_python_line_boundary_as_escaped_scalars(self) -> None:
        cases = (("inspect escape\x1bcharacter", "\\e"), ("inspect separator\u2028character", "\\L"))
        for item, escaped in cases:
            with self.subTest(item=item.encode("unicode_escape")), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                task = root / "task.md"
                task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")

                self.assertEqual(0, run(Args(root, Path("task.md"), 10, Path("task.md"), (item,), False)))

                text = task.read_text(encoding="utf-8")
                self.assertIn(escaped, text.split("---", 2)[1])
                metadata = parse_task_metadata(text, root)
                self.assertIsNotNone(metadata)
                assert metadata is not None
                self.assertEqual((item,), metadata.pending_task_items)

    def test_records_items_in_separate_target_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=("existing item",)) + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), False))

            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertIn("pending_task_items:\n  - existing item\n  - new worker item\n", worker.read_text(encoding="utf-8"))

    def test_repeated_separate_target_recording_does_not_duplicate_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=("existing item", "new worker item")) + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), False))

            self.assertEqual(0, exit_code)
            self.assertNotIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertEqual(1, worker.read_text(encoding="utf-8").count("new worker item"))

    def test_rejects_done_target_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(status="done") + "body\n", encoding="utf-8")

            exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("new worker item",), False))

            self.assertEqual(2, exit_code)
            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertIn("pending_task_items: []", worker.read_text(encoding="utf-8"))

    def test_rejects_missing_pending_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "not pending\n", encoding="utf-8")

            exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("finish review",), False))

            self.assertEqual(2, exit_code)
            self.assertIn("pending_task_items: []", task.read_text(encoding="utf-8"))

    def test_ack_human_reuses_previous_thread_with_exact_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            email = human_email(root)
            stdout = io.StringIO()
            commands: list[list[str]] = []
            bodies: list[str] = []

            def fake_run(command: list[str], check: bool) -> None:
                commands.append(command)
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fake_run), redirect_stdout(stdout):
                exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, email))

            self.assertEqual(0, exit_code)
            self.assertEqual(1, len(commands))
            self.assertIn("--manager-human", commands[0])
            self.assertNotIn("--subject-file", commands[0])
            self.assertIn("--message-file", commands[0])
            self.assertEqual("pending item created:\n- finish review\n", bodies[0])
            self.assertNotIn("task.md", bodies[0])
            self.assertIn("recorded 1 pending item", stdout.getvalue())

    def test_ack_human_honors_explicit_no_contact_without_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "Never email the Human.\n(pending)\nPlease do it.\n", encoding="utf-8")
            line = task.read_text(encoding="utf-8").splitlines().index("(pending)") + 1
            email = human_email(root)
            with patch("omo_manager.omo_record_pending.subprocess.run") as send:
                exit_code = run(Args(root, Path("task.md"), line, Path("task.md"), ("🧑 finish review",), True, email))
            self.assertEqual(0, exit_code)
            send.assert_not_called()
            self.assertIn("🧑 finish review", task.read_text(encoding="utf-8"))

    def test_ack_human_honors_source_email_no_contact_without_mail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            email = human_email(root)
            (root / email).write_text("Subject: Request\n\nNever email the Human.\n", encoding="utf-8")
            with patch("omo_manager.omo_record_pending.subprocess.run") as send:
                self.assertEqual(0, run(Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, email)))
            send.assert_not_called()

    def test_ack_human_requires_email_source_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter() + "(pending)\nPlease do it.\n"
            task.write_text(original, encoding="utf-8")

            self.assertEqual(2, run(Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True)))

            self.assertEqual(original, task.read_text(encoding="utf-8"))

    def test_ack_human_uses_email_file_only_as_verified_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            mail = root / "manager_mail" / "4002.txt"
            mail.parent.mkdir()
            mail.write_text("Subject: Re: PB review request\n\nPlease do it.\n", encoding="utf-8")
            bodies: list[str] = []

            def fake_run(command: list[str], check: bool) -> None:
                bodies.append(Path(command[command.index("--message-file") + 1]).read_text(encoding="utf-8"))

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fake_run):
                exit_code = run(Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 Please do it.",), True, Path("manager_mail/4002.txt")))

            self.assertEqual(0, exit_code)
            self.assertEqual(["pending item created:\n- Please do it.\n"], bodies)

    def test_ack_human_retry_succeeds_after_email_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, human_email(root))

            def fail_email(command: list[str], check: bool) -> None:
                raise subprocess.CalledProcessError(1, command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))

            text_after_failure = task.read_text(encoding="utf-8")
            self.assertIn("(pending)\n", text_after_failure)
            self.assertNotIn("  - 🧑 finish review\n", text_after_failure)
            commands: list[list[str]] = []
            stdout = io.StringIO()

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email), redirect_stdout(stdout):
                self.assertEqual(0, run(args))

            self.assertEqual(1, len(commands))
            self.assertIn("--manager-human", commands[0])
            self.assertIn("removed `(pending)`", stdout.getvalue())

    def test_ack_human_retry_rejects_unrelated_later_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, human_email(root))

            def fail_email(command: list[str], check: bool) -> None:
                raise subprocess.CalledProcessError(1, command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))
            task.write_text(task.read_text(encoding="utf-8") + "(pending)\nNew unrelated request.\n", encoding="utf-8")
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                self.assertEqual(2, run(args))

            self.assertEqual(0, len(commands))
            self.assertIn("(pending)\nNew unrelated request.\n", task.read_text(encoding="utf-8"))

    def test_ack_human_retry_rejects_shifted_live_pending_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "inserted line\n(pending)\nRoute this.\n", encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=("new worker item",)) + "body\n", encoding="utf-8")
            email = human_email(root)
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), ("🧑 new worker item",), True, email))

            self.assertEqual(2, exit_code)
            self.assertEqual([], commands)
            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))

    def test_ack_human_retry_rejects_new_marker_at_recorded_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            items = ("🧑 new worker item",)
            original = task_frontmatter(is_manager=True) + "(pending)\nNew unrelated request.\n" + recorded_line(10, items) + "\n"
            manager.write_text(original, encoding="utf-8")
            worker.write_text(task_frontmatter(pending_items=items) + "body\n", encoding="utf-8")
            email = human_email(root)
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                exit_code = run(Args(root, Path("manager.md"), 10, Path("worker.md"), items, True, email))

            self.assertEqual(2, exit_code)
            self.assertEqual([], commands)
            self.assertEqual(original, manager.read_text(encoding="utf-8"))

    def test_ack_human_retry_succeeds_after_oserror_email_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, human_email(root))

            def fail_email(command: list[str], check: bool) -> None:
                raise OSError("mail helper missing")

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=fail_email):
                self.assertEqual(2, run(args))
            text_after_failure = task.read_text(encoding="utf-8")
            self.assertIn("(pending)\n", text_after_failure)
            self.assertNotIn(recorded_line(10, ("🧑 finish review",)), text_after_failure)
            self.assertNotIn(ack_sent_line(10, ("🧑 finish review",)), text_after_failure)
            commands: list[list[str]] = []

            def send_email(command: list[str], check: bool) -> None:
                commands.append(command)

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                self.assertEqual(0, run(args))

            self.assertEqual(1, len(commands))
            self.assertIn(ack_sent_line(10, ("🧑 finish review",)), task.read_text(encoding="utf-8"))

    def test_ack_delivery_survives_mutation_failure_without_replay(self) -> None:
        from omo_manager.omo_record_pending import replace_if_unchanged_locked as actual_replace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, human_email(root))
            calls = 0

            def fail_mutation(path: Path, updated: str, before: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated mutation failure")
                actual_replace(path, updated, before)  # type: ignore[arg-type]

            with patch("omo_manager.omo_record_pending.subprocess.run") as send, patch(
                "omo_manager.omo_record_pending.replace_if_unchanged_locked", side_effect=fail_mutation
            ):
                self.assertEqual(2, run(args))

            send.assert_called_once()
            self.assertIn("(pending)\n", task.read_text(encoding="utf-8"))
            self.assertIn(ack_sent_line(10, args.items), task.read_text(encoding="utf-8"))

            with patch("omo_manager.omo_record_pending.subprocess.run") as retry_send:
                self.assertEqual(0, run(args))

            retry_send.assert_not_called()
            self.assertNotIn("(pending)\n", task.read_text(encoding="utf-8"))
            self.assertIn("  - 🧑 finish review\n", task.read_text(encoding="utf-8"))

    def test_post_send_marker_failure_retries_same_notice_identity(self) -> None:
        from omo_manager.omo_record_pending import replace_if_unchanged_locked as actual_replace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            task.write_text(task_frontmatter() + "(pending)\nPlease do it.\n", encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 finish review",), True, human_email(root))
            failed = False

            def fail_marker_once(path: Path, updated: str, before: object) -> None:
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("simulated marker failure")
                actual_replace(path, updated, before)  # type: ignore[arg-type]

            with patch("omo_manager.omo_record_pending.subprocess.run") as first_send, patch(
                "omo_manager.omo_record_pending.replace_if_unchanged_locked", side_effect=fail_marker_once
            ):
                self.assertEqual(2, run(args))

            self.assertIn("(pending)\n", task.read_text(encoding="utf-8"))
            with patch("omo_manager.omo_record_pending.subprocess.run") as retry_send:
                self.assertEqual(0, run(args))

            first_command = first_send.call_args.args[0]
            retry_command = retry_send.call_args.args[0]
            self.assertEqual(
                first_command[first_command.index("--pending-notice-key") + 1],
                retry_command[retry_command.index("--pending-notice-key") + 1],
            )
            self.assertNotIn("(pending)\n", task.read_text(encoding="utf-8"))

    def test_post_send_race_cannot_consume_a_changed_ingress_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter() + "(pending)\nFirst request.\n"
            task.write_text(original, encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 first request",), True, human_email(root))
            with patch("omo_manager.omo_record_pending.subprocess.run"), patch(
                "omo_manager.omo_record_pending.replace_if_unchanged_locked", side_effect=OSError("simulated marker race")
            ):
                self.assertEqual(2, run(args))

            changed = task_frontmatter() + "(pending)\nDifferent request.\n"
            task.write_text(changed, encoding="utf-8")
            with patch("omo_manager.omo_record_pending.subprocess.run") as retry_send:
                self.assertEqual(2, run(args))

            retry_send.assert_not_called()
            self.assertEqual(changed, task.read_text(encoding="utf-8"))

    def test_source_change_during_delivery_blocks_marker_and_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task.md"
            original = task_frontmatter() + "(pending)\nFirst request.\n"
            changed = task_frontmatter() + "(pending)\nDifferent request.\n"
            task.write_text(original, encoding="utf-8")
            args = Args(root, Path("task.md"), 10, Path("task.md"), ("🧑 first request",), True, human_email(root))

            def change_source(_command: list[str], check: bool) -> None:
                task.write_text(changed, encoding="utf-8")

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=change_source):
                self.assertEqual(2, run(args))

            self.assertEqual(changed, task.read_text(encoding="utf-8"))

    def test_target_change_at_delivery_boundary_blocks_email_and_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute it.\n", encoding="utf-8")
            original_target = task_frontmatter() + "body\n"
            changed_target = task_frontmatter(pending_items=("other work",)) + "body\n"
            worker.write_text(original_target, encoding="utf-8")
            args = Args(root, Path("manager.md"), 10, Path("worker.md"), ("🧑 routed request",), True, human_email(root))

            def change_target(_path: Path) -> str:
                worker.write_text(changed_target, encoding="utf-8")
                return "Re: Human request"

            with patch("omo_manager.omo_record_pending.subject_from_email_file", side_effect=change_target), patch(
                "omo_manager.omo_record_pending.subprocess.run"
            ) as send:
                self.assertEqual(2, run(args))

            send.assert_not_called()
            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertEqual(changed_target, worker.read_text(encoding="utf-8"))

    def test_target_change_during_delivery_blocks_marker_and_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute it.\n", encoding="utf-8")
            changed_target = task_frontmatter(pending_items=("other work",)) + "body\n"
            worker.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            args = Args(root, Path("manager.md"), 10, Path("worker.md"), ("🧑 routed request",), True, human_email(root))

            def change_target(_command: list[str], check: bool) -> None:
                worker.write_text(changed_target, encoding="utf-8")

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=change_target):
                self.assertEqual(2, run(args))

            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertEqual(changed_target, worker.read_text(encoding="utf-8"))

    def test_competing_supported_writer_waits_until_delivery_and_mutation_finish(self) -> None:
        from omo_manager.omo_task_lock import task_file_lock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute it.\n", encoding="utf-8")
            worker.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            args = Args(root, Path("manager.md"), 10, Path("worker.md"), ("🧑 routed request",), True, human_email(root))
            attempted = threading.Event()
            acquired = threading.Event()
            contender: threading.Thread | None = None

            def compete() -> None:
                attempted.set()
                with task_file_lock(worker):
                    acquired.set()

            def send_email(_command: list[str], check: bool) -> None:
                nonlocal contender
                contender = threading.Thread(target=compete)
                contender.start()
                self.assertTrue(attempted.wait(1))
                self.assertFalse(acquired.wait(0.05))

            with patch("omo_manager.omo_record_pending.subprocess.run", side_effect=send_email):
                self.assertEqual(0, run(args))
            assert contender is not None
            contender.join(1)
            self.assertTrue(acquired.is_set())
            self.assertIn("  - 🧑 routed request\n", worker.read_text(encoding="utf-8"))

    def test_cross_task_retry_accepts_its_own_partial_target_write(self) -> None:
        from omo_manager.omo_record_pending import replace_if_unchanged_locked as actual_replace

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "manager.md"
            worker = root / "worker.md"
            manager.write_text(task_frontmatter(is_manager=True) + "(pending)\nRoute it.\n", encoding="utf-8")
            worker.write_text(task_frontmatter() + "body\n", encoding="utf-8")
            args = Args(root, Path("manager.md"), 10, Path("worker.md"), ("🧑 routed request",), True, human_email(root))
            calls = 0

            def fail_source_once(path: Path, updated: str, before: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("simulated source failure")
                actual_replace(path, updated, before)  # type: ignore[arg-type]

            with patch("omo_manager.omo_record_pending.subprocess.run") as send, patch(
                "omo_manager.omo_record_pending.replace_if_unchanged_locked", side_effect=fail_source_once
            ):
                self.assertEqual(2, run(args))
            send.assert_called_once()
            self.assertIn("  - 🧑 routed request\n", worker.read_text(encoding="utf-8"))
            self.assertIn("(pending)\n", manager.read_text(encoding="utf-8"))

            with patch("omo_manager.omo_record_pending.subprocess.run") as retry_send:
                self.assertEqual(0, run(args))
            retry_send.assert_not_called()
            self.assertNotIn("(pending)\n", manager.read_text(encoding="utf-8"))
            self.assertEqual(1, worker.read_text(encoding="utf-8").count("🧑 routed request"))


if __name__ == "__main__":
    unittest.main()
