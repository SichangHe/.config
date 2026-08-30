from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import override
from unittest.mock import patch

import omo_manager.omo_worker_successor as worker_successor
from omo_manager.omo_task_metadata import parse_task_metadata
from omo_manager.omo_worker_successor import Args, SuccessorError, binding_from_committed_journal, launch_manifest_bytes, prepare_successor

TARGET = "testcfg:7.0"
MANAGER = "testcfg:1.0"
PROTECTED = ("othercfg:2.0",)
QUEUE = (
    "Preserve the exact inherited queue and finish the bounded correction.",
    "Return one immutable handoff after independent review.",
)
PROMPT = b"Implement only the bounded ordinary-worker correction.\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def queue_sha(items: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(items).encode()).hexdigest()


def task_text(
    *,
    status: str = "blocked",
    runat: str = TARGET,
    managerat: str = MANAGER,
    tool: str = "cursor",
    pending: tuple[str, ...] = QUEUE,
    is_manager: bool = False,
) -> str:
    pending_text = "pending_task_items: []" if not pending else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in pending)
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        "blocked_on: bounded replacement prerequisite\n"
        f"runat: {runat}\n"
        f"tool: {tool}\n"
        f"managerat: {managerat}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        f"{pending_text}\n"
        "---\n"
        "Existing evidence stays attached to the superseded record.\n"
    )


def todo_text() -> str:
    return (
        "current:\n"
        f"old_worker.md {TARGET}\n\n"
        "human pending:\n\n"
        "low priority:\n\n"
        "previous:\n"
    )


class WorkerSuccessorTests(unittest.TestCase):
    panes = patch.object(worker_successor, "pane_inventory", return_value={})

    @override
    def setUp(self) -> None:
        _ = self.panes.start()

    @override
    def tearDown(self) -> None:
        self.panes.stop()

    def make_root(self, root: Path, *, old: str | None = None) -> Args:
        old_text = task_text() if old is None else old
        todo = todo_text()
        (root / "old_worker.md").write_text(old_text, encoding="utf-8")
        (root / "TODO.md").write_text(todo, encoding="utf-8")
        prompt = root.parent / f"{root.name}.prompt"
        prompt.write_bytes(PROMPT)
        prompt.chmod(0o600)
        journal = root / ".omo-worker-successor-0123456789abcdef.transaction"
        workdir = root.parent / f"{root.name}.project"
        workdir.mkdir()
        manifest = root / ".omo-worker-successor-launch.json"
        manifest_data = launch_manifest_bytes(
            root=root,
            task_file="new_worker.md",
            target=TARGET,
            manager_target=MANAGER,
            tool="cursor",
            workdir=workdir,
            model="cursor-grok-4.6",
            reasoning_effort="xhigh",
        )
        manifest.write_bytes(manifest_data)
        manifest.chmod(0o600)
        return Args(
            root,
            "old_worker.md",
            "new_worker.md",
            TARGET,
            MANAGER,
            "cursor",
            sha(old_text.encode()),
            sha(todo.encode()),
            QUEUE,
            queue_sha(QUEUE),
            prompt,
            sha(PROMPT),
            PROTECTED,
            worker_successor.protected_digest(PROTECTED),
            journal,
            manifest,
            sha(manifest_data),
        )

    def assert_committed(self, args: Args) -> None:
        old = parse_task_metadata((args.root / args.old_task).read_text(encoding="utf-8"), args.root)
        successor = parse_task_metadata((args.root / args.successor_task).read_text(encoding="utf-8"), args.root)
        self.assertIsNotNone(old)
        self.assertIsNotNone(successor)
        assert old is not None and successor is not None
        self.assertEqual("done", old.status)
        self.assertEqual((), old.pending_task_items)
        self.assertEqual("blocked", successor.status)
        self.assertEqual(QUEUE, successor.pending_task_items)
        self.assertEqual(TARGET, successor.runat)
        self.assertEqual(MANAGER, successor.managerat)
        self.assertIn("old_worker.md testcfg:7.0", (args.root / "TODO.md").read_text(encoding="utf-8").split("previous:\n", 1)[1])
        self.assertIn("new_worker.md testcfg:7.0", (args.root / "TODO.md").read_text(encoding="utf-8"))
        self.assertEqual("committed", __import__("json").loads(args.journal.read_bytes())["phase"])

    def test_prepares_exact_blocked_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            args = self.make_root(root)

            result = prepare_successor(args)

            self.assertIn("prepared blocked successor", result)
            self.assert_committed(args)
            successor = (root / "new_worker.md").read_bytes()
            binding = binding_from_committed_journal(
                args.journal,
                expected_journal_sha256=sha(args.journal.read_bytes()),
                expected_task_sha256=sha(successor),
                expected_prompt_sha256=args.prompt_sha256,
                expected_queue_sha256=args.queue_sha256,
                expected_launch_manifest_sha256=args.launch_manifest_sha256,
            )
            self.assertEqual(PROMPT, binding.prompt_data)
            self.assertEqual(QUEUE, binding.queue)

    def test_retries_committed_transaction_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            args = self.make_root(root)
            _ = prepare_successor(args)
            before = {path.name: path.read_bytes() for path in (root / "old_worker.md", root / "new_worker.md", root / "TODO.md", args.journal)}

            _ = prepare_successor(args)

            self.assertEqual(before, {path.name: path.read_bytes() for path in (root / "old_worker.md", root / "new_worker.md", root / "TODO.md", args.journal)})

    def test_recovers_each_interrupted_journal_phase(self) -> None:
        class Crash(RuntimeError):
            pass

        for phase in worker_successor.CRASH_PHASES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "work_logs"
                root.mkdir()
                args = self.make_root(root)

                with patch.object(worker_successor, "maybe_crash", side_effect=lambda value, phase=phase: (_ for _ in ()).throw(Crash()) if value == phase else None), self.assertRaises(Crash):
                    _ = prepare_successor(args)

                _ = prepare_successor(args)
                self.assert_committed(args)

    def test_rejects_symlink_root_and_successor_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "work_logs"
            root.mkdir()
            args = self.make_root(root)
            alias = base / "alias"
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(SuccessorError, "canonical"):
                _ = prepare_successor(replace(args, root=alias, journal=alias / args.journal.name))
            (root / "new_worker.md").write_text("foreign\n", encoding="utf-8")
            with self.assertRaisesRegex(SuccessorError, "already exists"):
                _ = prepare_successor(args)
            self.assertEqual("foreign\n", (root / "new_worker.md").read_text(encoding="utf-8"))

    def test_rejects_role_manager_tool_target_queue_and_digest_mismatch(self) -> None:
        cases = {
            "manager": task_text(managerat="testcfg:2.0"),
            "tool": task_text(tool="codex"),
            "target": task_text(runat="testcfg:8.0"),
            "manager-role": task_text(is_manager=True),
            "queue": task_text(pending=(QUEUE[0],)),
        }
        for label, old in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "work_logs"
                root.mkdir()
                args = self.make_root(root, old=old)
                with self.assertRaisesRegex(SuccessorError, "exact role|queue"):
                    _ = prepare_successor(args)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            args = self.make_root(root)
            with self.assertRaisesRegex(SuccessorError, "digest changed"):
                _ = prepare_successor(replace(args, old_sha256="0" * 64))

    def test_rejects_prompt_swap_before_prepare_and_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            args = self.make_root(root)
            args.prompt_file.write_bytes(b"swapped\n")
            with self.assertRaisesRegex(SuccessorError, "prompt digest"):
                _ = prepare_successor(args)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            args = self.make_root(root)
            _ = prepare_successor(args)
            task_sha = sha((root / "new_worker.md").read_bytes())
            journal_sha = sha(args.journal.read_bytes())
            args.prompt_file.write_bytes(b"swapped after commit\n")
            with self.assertRaisesRegex(SuccessorError, "swapped"):
                _ = binding_from_committed_journal(
                    args.journal,
                    expected_journal_sha256=journal_sha,
                    expected_task_sha256=task_sha,
                    expected_prompt_sha256=args.prompt_sha256,
                    expected_queue_sha256=args.queue_sha256,
                    expected_launch_manifest_sha256=args.launch_manifest_sha256,
                )

    def test_rejects_duplicate_or_malformed_global_owner(self) -> None:
        for malformed in (False, True):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "work_logs"
                root.mkdir()
                args = self.make_root(root)
                duplicate = task_text(managerat="testcfg:3.0")
                if malformed:
                    duplicate = duplicate.replace(f"runat: {TARGET}", f"runat: {TARGET} # ambiguous")
                (root / "duplicate.md").write_text(duplicate, encoding="utf-8")
                with self.assertRaisesRegex(Exception, "sole authoritative|sole active|invalid frontmatter"):
                    _ = prepare_successor(args)

    def test_rejects_live_target_and_protected_target_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "work_logs"
            root.mkdir()
            args = self.make_root(root)
            with patch.object(worker_successor, "pane_inventory", return_value={TARGET: object()}), self.assertRaisesRegex(SuccessorError, "target is live"):
                _ = prepare_successor(args)
            with self.assertRaisesRegex(SystemExit, "2"):
                _ = worker_successor.parse_args(
                    [
                        "--root", str(root), "--old-task", args.old_task, "--successor-task", args.successor_task,
                        "--target", TARGET, "--manager-target", MANAGER, "--tool", "cursor",
                        "--old-sha256", args.old_sha256, "--todo-sha256", args.todo_sha256,
                        "--expected-pending-item", QUEUE[0], "--expected-pending-item", QUEUE[1], "--queue-sha256", args.queue_sha256,
                        "--prompt-file", str(args.prompt_file), "--prompt-sha256", args.prompt_sha256,
                        "--protected-target", TARGET, "--protected-sha256", worker_successor.protected_digest((TARGET,)),
                        "--journal", str(args.journal),
                        "--launch-manifest", str(args.launch_manifest), "--launch-manifest-sha256", args.launch_manifest_sha256,
                    ]
                )

    def test_real_subprocess_crash_and_concurrent_retry_are_idempotent(self) -> None:
        for phase in worker_successor.CRASH_PHASES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "work_logs"
                root.mkdir()
                args = self.make_root(root)
                argv = [
                    str(Path(sys.executable)),
                    "-m", "omo_manager.omo_worker_successor",
                    "--root", str(root), "--old-task", args.old_task, "--successor-task", args.successor_task,
                    "--target", TARGET, "--manager-target", MANAGER, "--tool", "cursor",
                    "--old-sha256", args.old_sha256, "--todo-sha256", args.todo_sha256,
                    "--expected-pending-item", QUEUE[0], "--expected-pending-item", QUEUE[1], "--queue-sha256", args.queue_sha256,
                    "--prompt-file", str(args.prompt_file), "--prompt-sha256", args.prompt_sha256,
                    "--protected-target", PROTECTED[0], "--protected-sha256", args.protected_sha256,
                    "--journal", str(args.journal),
                    "--launch-manifest", str(args.launch_manifest), "--launch-manifest-sha256", args.launch_manifest_sha256,
                ]
                env = {**os.environ, "OMO_WORKER_SUCCESSOR_CRASH_AFTER": phase}
                crashed = subprocess.run(argv, cwd=Path(__file__).parents[2], env=env, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(86, crashed.returncode)
                first = subprocess.Popen(argv, cwd=Path(__file__).parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                second = subprocess.Popen(argv, cwd=Path(__file__).parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                first_out, first_err = first.communicate(timeout=30)
                second_out, second_err = second.communicate(timeout=30)
                self.assertEqual((0, 0), (first.returncode, second.returncode), (first_out, first_err, second_out, second_err))
                self.assert_committed(args)


if __name__ == "__main__":
    unittest.main()
