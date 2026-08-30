from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import omo_manager.omo_manager_replace as manager_replace
from omo_manager.omo_manager_replace import Args, ChildPin, LineRange, PaneIdentity, ReplaceError, pane_inventory, parse_args, replace_manager
from omo_manager.omo_task_metadata import TaskMetadata, parse_task_metadata

OLD_TARGET = "private_mgr:1"
NEW_TARGET = "private_mgr:3"
PARENT_TARGET = "portfolio:0"
SESSION_ID = "11111111-2222-4333-8444-555555555555"
OLD_QUEUE = ("Finish the bounded experiment.", "Return the post-analysis.")
AUTHORITY_LINES = (
    "Subject: Re: Verus experiments\n",
    "\n",
    "For a manager, keep the request straightforward.\n",
    "The agent failed. They did not run the experiment. Replace them.\n",
    "The replacement agent should finish the task.\n",
)


def sha(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def task_text(
    *,
    status: str,
    runat: str,
    managerat: str,
    is_manager: bool,
    pending: tuple[str, ...],
    session_id: str = "",
    body: str = "Preserve this delegated body.\n",
) -> str:
    blocker = "blocked_on: fixture blocker\n" if status == "blocked" else ""
    session = f"session_id: {session_id}\n" if session_id else ""
    queue = "pending_task_items: []" if not pending else "pending_task_items:\n" + "\n".join(f"  - {item}" for item in pending)
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocker}"
        f"runat: {runat}\n"
        "tool: codex\n"
        f"managerat: {managerat}\n"
        f"is_manager: {str(is_manager).lower()}\n"
        f"{queue}\n"
        f"{session}"
        "---\n"
        f"{body}"
    )


def parsed(path: Path, root: Path) -> TaskMetadata:
    value = parse_task_metadata(path.read_text(encoding="utf-8"), root)
    if value is None:
        raise AssertionError("expected task metadata")
    return value


class ManagerReplaceTests(unittest.TestCase):
    def fixture(self, base: Path) -> tuple[Path, Args, dict[str, str]]:
        root = base / "work_logs"
        root.mkdir(mode=0o700)
        private = base / "private"
        private.mkdir(mode=0o700)
        files = {
            "failed_manager.md": task_text(
                status="long_running",
                runat=OLD_TARGET,
                managerat=PARENT_TARGET,
                is_manager=True,
                pending=OLD_QUEUE,
                session_id=SESSION_ID,
            ),
            "child_a.md": task_text(
                status="running",
                runat="worker:1",
                managerat=OLD_TARGET,
                is_manager=False,
                pending=("Translate one module.",),
            ),
            "child_b.md": task_text(
                status="blocked",
                runat="worker:2",
                managerat=OLD_TARGET,
                is_manager=False,
                pending=("Run the verifier.",),
            ),
            "unrelated.md": task_text(
                status="running",
                runat="other:1",
                managerat="other_mgr:0",
                is_manager=False,
                pending=("Remain unchanged.",),
            ),
            "TODO.md": (
                "current:\n"
                "failed_manager.md private_mgr:1\n"
                "unrelated.md other:1\n\n"
                "human pending:\n\n"
                "low priority:\n\n"
                "previous:\n"
            ),
        }
        for name, data in files.items():
            (root / name).write_text(data, encoding="utf-8")
        authority = root / "manager_mail" / "source-1220.txt"
        authority.parent.mkdir(mode=0o700)
        authority.write_text("".join(AUTHORITY_LINES), encoding="utf-8")
        authority.chmod(0o600)
        files["manager_mail/source-1220.txt"] = "".join(AUTHORITY_LINES)
        envelope_text = (
            '<human_instruction authoritative="true" source="manager_mail/source-1220.txt:1-5">\n'
            f'{"".join(AUTHORITY_LINES)}'
            "</human_instruction>\n"
        )
        (root / "authority_envelope.md").write_text(envelope_text, encoding="utf-8")
        files["authority_envelope.md"] = envelope_text
        args = Args(
            root=root,
            old_task="failed_manager.md",
            successor_task="successor_manager.md",
            old_target=OLD_TARGET,
            new_target=NEW_TARGET,
            parent_target=PARENT_TARGET,
            old_sha256=sha(files["failed_manager.md"]),
            todo_sha256=sha(files["TODO.md"]),
            children=(ChildPin("child_a.md", sha(files["child_a.md"])), ChildPin("child_b.md", sha(files["child_b.md"]))),
            old_pane_id="%42",
            old_pane_pid=4242,
            old_pane_start_ticks=999,
            old_session_id=SESSION_ID,
            authority_file="manager_mail/source-1220.txt",
            authority_lines=LineRange(1, 5),
            authority_sha256=sha(files["manager_mail/source-1220.txt"]),
            authority_envelope_task="authority_envelope.md",
            authority_envelope_sha256=sha(files["authority_envelope.md"]),
            successor_item_lines=(LineRange(3, 5),),
            protected_targets=("human_data:9",),
            audit_output=private / "manager-replace.json",
            preparer="setup-agent",
            reviewer="independent-reviewer",
        )
        return root, args, files

    def runtime(self, state: dict[str, bool]):
        def inventory() -> dict[str, PaneIdentity]:
            result: dict[str, PaneIdentity] = {}
            if state.get("old_live", True):
                result[manager_replace.canonical_target(OLD_TARGET)] = PaneIdentity(
                    manager_replace.canonical_target(OLD_TARGET), "%42", 4242, 999
                )
            if state.get("new_live", False):
                result[manager_replace.canonical_target(NEW_TARGET)] = PaneIdentity(
                    manager_replace.canonical_target(NEW_TARGET), "%77", 7777, 1001
                )
            return result

        def stopped(_args: object) -> str:
            state["old_live"] = False
            hook = state.get("stop_hook")
            if callable(hook):
                hook()
            return SESSION_ID

        return (
            patch.object(manager_replace, "pane_inventory", side_effect=inventory),
            patch.object(manager_replace, "stop", side_effect=stopped),
            patch.object(manager_replace, "has_bound_close_proof", side_effect=lambda *_args: not state.get("old_live", True)),
        )

    def run_replacement(self, args: Args, state: dict[str, bool]) -> str:
        inventory, stopped, proof = self.runtime(state)
        with inventory, stopped, proof:
            return replace_manager(args)

    def test_success_closes_migrates_and_publishes_unlaunched_successor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            result = self.run_replacement(args, {"old_live": True})

            old = parsed(root / args.old_task, root)
            successor = parsed(root / args.successor_task, root)
            self.assertEqual("done", old.status)
            self.assertEqual((), old.pending_task_items)
            self.assertEqual("blocked", successor.status)
            self.assertEqual(NEW_TARGET, successor.runat)
            self.assertEqual(3, len(successor.pending_task_items))
            self.assertEqual(OLD_QUEUE, successor.pending_task_items[:2])
            self.assertIn("manager_mail/source-1220.txt:3-5", successor.pending_task_items[-1])
            self.assertNotIn("The agent failed", (root / args.successor_task).read_text(encoding="utf-8"))
            self.assertEqual("", successor.session_id)
            self.assertEqual(NEW_TARGET, parsed(root / "child_a.md", root).managerat)
            self.assertEqual(NEW_TARGET, parsed(root / "child_b.md", root).managerat)
            self.assertEqual(("Translate one module.",), parsed(root / "child_a.md", root).pending_task_items)
            self.assertEqual(("Run the verifier.",), parsed(root / "child_b.md", root).pending_task_items)
            self.assertEqual(files["unrelated.md"], (root / "unrelated.md").read_text(encoding="utf-8"))
            todo = (root / "TODO.md").read_text(encoding="utf-8")
            self.assertIn("successor_manager.md private_mgr:3", todo)
            self.assertIn("previous:\nfailed_manager.md private_mgr:1", todo)
            audit = json.loads(args.audit_output.read_text(encoding="utf-8"))
            self.assertEqual("committed", audit["state"])
            self.assertEqual(["failed_manager.md", "child_a.md", "child_b.md", "TODO.md", "successor_manager.md"], audit["completed_writes"])
            self.assertIn("launch remains a separate supported operation", result)

    def test_stale_task_todo_or_child_digest_rejects_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            cases = (
                replace(args, old_sha256="0" * 64),
                replace(args, todo_sha256="0" * 64),
                replace(args, children=(ChildPin("child_a.md", "0" * 64), args.children[1])),
            )
            for index, changed in enumerate(cases):
                with self.subTest(index=index):
                    state = {"old_live": True}
                    inventory, stopped, proof = self.runtime(state)
                    with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "digest changed"):
                        replace_manager(changed)
                    stop_mock.assert_not_called()
                    self.assertFalse(args.audit_output.exists())
                    self.assertEqual(files["failed_manager.md"], (root / "failed_manager.md").read_text(encoding="utf-8"))

    def test_authority_source_and_envelope_are_digest_bound_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            cases = (
                replace(args, authority_sha256="0" * 64),
                replace(args, authority_envelope_sha256="0" * 64),
            )
            for changed in cases:
                with self.subTest(changed=changed):
                    inventory, stopped, proof = self.runtime({"old_live": True})
                    with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "authority.*digest"):
                        replace_manager(changed)
                    stop_mock.assert_not_called()

            malformed = "Agent-authored routing text without an authoritative envelope.\n"
            (root / args.authority_envelope_task).write_text(malformed, encoding="utf-8")
            malformed_args = replace(args, authority_envelope_sha256=sha(malformed))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "exactly one block"):
                replace_manager(malformed_args)
            stop_mock.assert_not_called()

    def test_authority_block_digest_ignores_unrelated_outer_envelope_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            original = (root / args.authority_envelope_task).read_text(encoding="utf-8")
            block = original.rstrip("\n")
            wrapped = (
                "manager-authentication-wrapper: v1\n"
                "routing-note: unrelated outer text\n\n"
                f"{block}\n"
                "postscript: unrelated outer text\n"
            )
            (root / args.authority_envelope_task).write_text(wrapped, encoding="utf-8")
            changed = replace(args, authority_envelope_sha256=sha(block + "\n"))
            plan = manager_replace.prepare(changed, manager_replace.markdown_paths(root))
            self.assertEqual(wrapped.encode(), plan.authority_envelope.data)
            self.assertIn("envelope-block-sha256", plan.successor_queue[-1])

    def test_parse_args_accepts_tmux_zero_pane_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = [
                "--root", str(root),
                "--old-task", "old.md",
                "--successor-task", "new.md",
                "--old-target", "mgr:1.0",
                "--new-target", "mgr:1.1",
                "--parent-target", "parent:0",
                "--old-sha256", "a" * 64,
                "--todo-sha256", "b" * 64,
                "--old-pane-id", "%0",
                "--old-pane-pid", "42",
                "--old-pane-start-ticks", "99",
                "--old-session-id", SESSION_ID,
                "--authority-file", "manager_mail/source.txt",
                "--authority-lines", "1-1",
                "--authority-sha256", "c" * 64,
                "--authority-envelope-task", "envelope.md",
                "--authority-envelope-sha256", "d" * 64,
                "--successor-item-lines", "1-1",
                "--audit-output", str(root / "audit.json"),
                "--preparer", "a",
                "--reviewer", "b",
            ]
            self.assertEqual("%0", parse_args(source).old_pane_id)

    def test_pane_inventory_accepts_tmux_zero_pane_id(self) -> None:
        result = manager_replace.subprocess.CompletedProcess(
            ["tmux", "list-panes"],
            0,
            "mgr:1.0\t%0\t42\t0\n",
            "",
        )
        with (
            patch.object(manager_replace.subprocess, "run", return_value=result),
            patch.object(manager_replace, "process_start_ticks", return_value=99),
        ):
            self.assertEqual(
                {"mgr:1.0": PaneIdentity("mgr:1.0", "%0", 42, 99)},
                pane_inventory(),
            )

    def test_pane_inventory_ignores_only_tmux_confirmed_dead_panes(self) -> None:
        result = manager_replace.subprocess.CompletedProcess(
            ["tmux", "list-panes"],
            0,
            "stale:0.0\t%1\t999\t1\n"
            "mgr:1.0\t%42\t42\t0\n",
            "",
        )
        with (
            patch.object(manager_replace.subprocess, "run", return_value=result),
            patch.object(manager_replace, "process_start_ticks", return_value=99) as ticks,
        ):
            self.assertEqual(
                {"mgr:1.0": PaneIdentity("mgr:1.0", "%42", 42, 99)},
                pane_inventory(),
            )
        ticks.assert_called_once_with(42)

    def test_pane_inventory_fails_closed_for_unprovable_live_pane(self) -> None:
        result = manager_replace.subprocess.CompletedProcess(
            ["tmux", "list-panes"],
            0,
            "mgr:1.0\t%42\t42\t0\n",
            "",
        )
        with (
            patch.object(manager_replace.subprocess, "run", return_value=result),
            patch.object(manager_replace, "process_start_ticks", return_value=None),
            self.assertRaisesRegex(ReplaceError, "cannot prove one process identity"),
        ):
            pane_inventory()

    def test_authenticated_authority_without_failure_evidence_cannot_close_healthy_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            benign_lines = (
                "Subject: status\n",
                "\n",
                "Keep the existing manager running.\n",
                "It completed the requested experiment.\n",
                "Do not replace it.\n",
            )
            source = "".join(benign_lines)
            envelope = (
                '<human_instruction authoritative="true" source="manager_mail/source-1220.txt:1-5">\n'
                f"{source}</human_instruction>\n"
            )
            (root / args.authority_file).write_text(source, encoding="utf-8")
            (root / args.authority_envelope_task).write_text(envelope, encoding="utf-8")
            changed = replace(args, authority_sha256=sha(source), authority_envelope_sha256=sha(envelope))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "does not explicitly prove"):
                replace_manager(changed)
            stop_mock.assert_not_called()

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            healthy = task_text(
                status="running",
                runat=OLD_TARGET,
                managerat=PARENT_TARGET,
                is_manager=True,
                pending=OLD_QUEUE,
                session_id=SESSION_ID,
            )
            (root / args.old_task).write_text(healthy, encoding="utf-8")
            changed = replace(args, old_sha256=sha(healthy))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "exact live long-running failed-manager"):
                replace_manager(changed)
            stop_mock.assert_not_called()

    def test_concurrent_todo_change_is_preserved_and_owned_writes_roll_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))

            def mutate_todo() -> None:
                (root / "TODO.md").write_text(files["TODO.md"] + "external concurrent note\n", encoding="utf-8")

            with self.assertRaisesRegex(ReplaceError, "owned lifecycle writes rolled back"):
                self.run_replacement(args, {"old_live": True, "stop_hook": mutate_todo})
            self.assertEqual(files["failed_manager.md"], (root / "failed_manager.md").read_text(encoding="utf-8"))
            self.assertEqual(files["child_a.md"], (root / "child_a.md").read_text(encoding="utf-8"))
            self.assertEqual(files["child_b.md"], (root / "child_b.md").read_text(encoding="utf-8"))
            self.assertTrue((root / "TODO.md").read_text(encoding="utf-8").endswith("external concurrent note\n"))
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_concurrent_old_task_change_rejects_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            changed = files["failed_manager.md"] + "external task note\n"

            def mutate_task() -> None:
                (root / "failed_manager.md").write_text(changed, encoding="utf-8")

            with self.assertRaisesRegex(ReplaceError, "owned lifecycle writes rolled back"):
                self.run_replacement(args, {"old_live": True, "stop_hook": mutate_task})
            self.assertEqual(changed, (root / "failed_manager.md").read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())

    def test_partial_child_migration_rolls_back_exact_owned_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            original = manager_replace.replace_snapshot

            def fail_second_child(expected, data, label):
                if label == "active child child_b.md":
                    raise ReplaceError("injected partial migration fault")
                return original(expected, data, label)

            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                inventory,
                stopped,
                proof,
                patch.object(manager_replace, "replace_snapshot", side_effect=fail_second_child),
                self.assertRaisesRegex(ReplaceError, "all lifecycle writes rolled back"),
            ):
                replace_manager(args)
            for name, data in files.items():
                self.assertEqual(data, (root / name).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_post_publication_proof_failure_removes_successor_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with (
                inventory,
                stopped,
                proof,
                patch.object(manager_replace, "prove_committed", side_effect=ReplaceError("injected proof fault")),
                self.assertRaisesRegex(ReplaceError, "all lifecycle writes rolled back"),
            ):
                replace_manager(args)
            for name, data in files.items():
                self.assertEqual(data, (root / name).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())

    def test_crash_after_partial_migration_recovers_to_one_committed_successor(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            original = manager_replace.replace_snapshot
            crashed = False

            def crash_after_first_child(expected, data, label):
                nonlocal crashed
                result = original(expected, data, label)
                if label == "active child child_a.md" and not crashed:
                    crashed = True
                    raise SimulatedCrash()
                return result

            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "replace_snapshot", side_effect=crash_after_first_child), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            self.assertFalse(state["old_live"])
            self.assertTrue(args.audit_output.exists())

            result = self.run_replacement(args, state)
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual("done", parsed(root / args.old_task, root).status)
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)
            self.assertEqual(NEW_TARGET, parsed(root / "child_a.md", root).managerat)
            self.assertEqual(NEW_TARGET, parsed(root / "child_b.md", root).managerat)

    def test_crash_after_audit_reservation_recovers_the_same_pinned_close(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "stop_old_manager", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            self.assertTrue(state["old_live"])
            self.assertEqual("prepared", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

            result = self.run_replacement(args, state)
            self.assertFalse(state["old_live"])
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_crash_between_audit_and_close_authority_recovers_without_a_second_owner(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            original = manager_replace.reserve_audit
            crashed = False

            def crash_after_main_audit(path, record):
                nonlocal crashed
                result = original(path, record)
                if path == args.audit_output and not crashed:
                    crashed = True
                    raise SimulatedCrash()
                return result

            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "reserve_audit", side_effect=crash_after_main_audit), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            close_authority = args.audit_output.with_name(f".{args.audit_output.name}.close-authority")
            self.assertTrue(args.audit_output.exists())
            self.assertFalse(close_authority.exists())
            self.assertTrue(state["old_live"])

            result = self.run_replacement(args, state)
            self.assertTrue(close_authority.exists())
            self.assertFalse(state["old_live"])
            self.assertIn("sole ownership", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_crash_after_all_writes_recovers_commit_without_second_stop(self) -> None:
        class SimulatedCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            state = {"old_live": True}
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped, proof, patch.object(manager_replace, "prove_committed", side_effect=SimulatedCrash), self.assertRaises(SimulatedCrash):
                replace_manager(args)
            inventory, stopped, proof = self.runtime(state)
            with inventory, stopped as stop_mock, proof:
                result = replace_manager(args)
            stop_mock.assert_not_called()
            self.assertIn("recovered committed", result)
            self.assertEqual("committed", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])
            self.assertEqual("blocked", parsed(root / args.successor_task, root).status)

    def test_atomic_exchange_preserves_a_concurrently_rebound_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.md"
            path.write_text("expected\n", encoding="utf-8")
            expected = manager_replace.read_snapshot(path, "fixture")
            original = manager_replace.rename_at2
            injected = False

            def rebind_before_exchange(parent_fd, source, target, flags):
                nonlocal injected
                if flags == manager_replace.RENAME_EXCHANGE and not injected:
                    injected = True
                    foreign = path.with_name("foreign.md")
                    foreign.write_text("foreign concurrent bytes\n", encoding="utf-8")
                    foreign.replace(path)
                return original(parent_fd, source, target, flags)

            with patch.object(manager_replace, "rename_at2", side_effect=rebind_before_exchange), self.assertRaisesRegex(ReplaceError, "concurrently rebound"):
                manager_replace.replace_snapshot(expected, b"transaction bytes\n", "fixture")
            self.assertEqual("foreign concurrent bytes\n", path.read_text(encoding="utf-8"))

    def test_atomic_successor_removal_preserves_a_concurrently_rebound_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "successor.md"
            path.write_text("transaction successor\n", encoding="utf-8")
            expected = manager_replace.read_snapshot(path, "fixture successor")
            original = manager_replace.rename_at2
            injected = False

            def rebind_before_remove(parent_fd, source, target, flags):
                nonlocal injected
                if flags == manager_replace.RENAME_NOREPLACE and source == path.name and not injected:
                    injected = True
                    foreign = path.with_name("foreign-successor.md")
                    foreign.write_text("foreign concurrent successor\n", encoding="utf-8")
                    foreign.replace(path)
                return original(parent_fd, source, target, flags)

            with patch.object(manager_replace, "rename_at2", side_effect=rebind_before_remove), self.assertRaisesRegex(ReplaceError, "rebound"):
                manager_replace.remove_created(expected)
            self.assertEqual("foreign concurrent successor\n", path.read_text(encoding="utf-8"))

    def test_late_orphan_child_is_detected_and_never_committed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, files = self.fixture(Path(tmp))
            orphan = task_text(
                status="running",
                runat="other:1",
                managerat=OLD_TARGET,
                is_manager=False,
                pending=("Concurrent child request.",),
            )

            def retarget_unrelated() -> None:
                (root / "unrelated.md").write_text(orphan, encoding="utf-8")

            with self.assertRaisesRegex(ReplaceError, "all lifecycle writes rolled back"):
                self.run_replacement(args, {"old_live": True, "stop_hook": retarget_unrelated})
            self.assertEqual(orphan, (root / "unrelated.md").read_text(encoding="utf-8"))
            self.assertEqual(files["failed_manager.md"], (root / args.old_task).read_text(encoding="utf-8"))
            self.assertFalse((root / args.successor_task).exists())
            self.assertEqual("rolled_back", json.loads(args.audit_output.read_text(encoding="utf-8"))["state"])

    def test_duplicate_owner_or_incomplete_child_set_rejects_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root, args, _files = self.fixture(Path(tmp))
            (root / "duplicate.md").write_text(
                task_text(status="running", runat=OLD_TARGET, managerat=PARENT_TARGET, is_manager=True, pending=("duplicate",)),
                encoding="utf-8",
            )
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "exactly one authoritative active owner"):
                replace_manager(args)
            stop_mock.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            incomplete = replace(args, children=(args.children[0],))
            inventory, stopped, proof = self.runtime({"old_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "active child set changed"):
                replace_manager(incomplete)
            stop_mock.assert_not_called()

    def test_human_or_explicitly_protected_target_rejects_without_pane_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            cases = (
                replace(args, old_target="hprivate:1"),
                replace(args, new_target="hprivate:3"),
                replace(args, protected_targets=("private_mgr:1.0",)),
            )
            for changed in cases:
                with self.subTest(target=(changed.old_target, changed.new_target, changed.protected_targets)):
                    with patch.object(manager_replace, "pane_inventory") as inventory, self.assertRaisesRegex(ReplaceError, "human-owned|protected"):
                        replace_manager(changed)
                    inventory.assert_not_called()

    def test_live_successor_target_rejects_launch_before_singular_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _root, args, _files = self.fixture(Path(tmp))
            inventory, stopped, proof = self.runtime({"old_live": True, "new_live": True})
            with inventory, stopped as stop_mock, proof, self.assertRaisesRegex(ReplaceError, "launch-before-singular-proof"):
                replace_manager(args)
            stop_mock.assert_not_called()
            self.assertFalse(args.audit_output.exists())


if __name__ == "__main__":
    unittest.main()
