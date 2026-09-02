from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_agent_status as status
from omo_manager import omo_external_task_register as registration
from omo_manager import omo_manager_env as manager_env


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task_text(*, runat: str = "mac_stt:3", managerat: str = "mac_stt:0", status_value: str = "blocked", blocked_on: str = "human") -> str:
    blocker = f"blocked_on: {blocked_on}\n" if blocked_on else ""
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status_value}\n"
        f"{blocker}"
        f"runat: {runat}\n"
        "tool: codex\n"
        f"managerat: {managerat}\n"
        "is_manager: false\n"
        "pending_task_items:\n"
        "  - Preserve the physical-Mac blocker.\n"
        "---\n"
        "Task body and immutable history.\n"
    )


class ExternalTaskRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "work_logs"
        self.external = base / "external"
        self.registry = base / "state" / "external-task-registrations"
        self.packet = base / "packet"
        for directory in (self.root, self.external, self.registry, self.packet):
            directory.mkdir(parents=True, mode=0o700)
            directory.chmod(0o700)
        environment = patch.dict(os.environ, {"OMO_EXTERNAL_TASK_REGISTRY": str(self.registry)})
        environment.start()
        self.addCleanup(environment.stop)
        configured = patch.dict(manager_env.CONFIGURED_ENV, {"OMO_EXTERNAL_TASK_REGISTRY": str(self.registry)})
        configured.start()
        self.addCleanup(configured.stop)
        self.task = self.external / "task.md"
        self.task_ref = "../external/task.md"
        self.todo = self.root / "TODO.md"
        self.task.write_text(task_text(), encoding="utf-8")
        self.todo.write_text(f"current:\n- `{self.task_ref}` mac_stt:3\n", encoding="utf-8")
        self.task.chmod(0o600)
        self.todo.chmod(0o600)
        self.plan_path = self.packet / "registration-plan.json"

    def test_registry_initialization_is_exact_private_and_idempotent(self) -> None:
        fresh = Path(self.temporary.name) / "fresh-state"
        fresh.mkdir(mode=0o700)
        fresh.chmod(0o700)
        registry = fresh / "registry"
        with patch.dict(manager_env.CONFIGURED_ENV, {"OMO_EXTERNAL_TASK_REGISTRY": str(registry)}):
            self.assertEqual(registry, registration.initialize_registry(registry))
            self.assertEqual(0o700, registry.stat().st_mode & 0o777)
            self.assertEqual(registry, registration.initialize_registry(registry))
        registry.rmdir()
        target = fresh / "target"
        target.mkdir(mode=0o700)
        registry.symlink_to(target, target_is_directory=True)
        with patch.dict(manager_env.CONFIGURED_ENV, {"OMO_EXTERNAL_TASK_REGISTRY": str(registry)}):
            with self.assertRaises(OSError):
                registration.initialize_registry(registry)

    def test_local_env_only_configuration_is_shared_by_cli_and_watcher(self) -> None:
        local_env = Path(self.temporary.name) / "local.env"
        local_env.write_text(f"OMO_EXTERNAL_TASK_REGISTRY={self.registry}\n", encoding="utf-8")
        original_registry = os.environ.pop("OMO_EXTERNAL_TASK_REGISTRY", None)
        try:
            with patch.dict(os.environ, {"OMO_MANAGER_LOCAL_ENV": str(local_env)}):
                loaded = manager_env.load_local_env()
        finally:
            if original_registry is not None:
                os.environ["OMO_EXTERNAL_TASK_REGISTRY"] = original_registry
        self.assertEqual(str(self.registry), loaded["OMO_EXTERNAL_TASK_REGISTRY"])
        with patch.dict(manager_env.CONFIGURED_ENV, loaded, clear=True):
            self.assertEqual(self.registry, registration.default_registry_dir())
            self.applied()
            self.assertEqual(self.task, status.resolve_task_path(self.root, self.task_ref))

    def plan(self) -> registration.RegistrationPlan:
        return registration.build_plan(
            self.root,
            self.task,
            self.task_ref,
            "mac_stt:3",
            "mac_stt:0",
            sha256(self.task),
            sha256(self.todo),
            self.registry,
        )

    def prepared(self) -> tuple[registration.RegistrationPlan, str]:
        plan = self.plan()
        registration.write_plan(self.plan_path, plan)
        return plan, sha256(self.plan_path)

    def applied(self) -> tuple[registration.RegistrationPlan, Path]:
        plan, plan_digest = self.prepared()
        return plan, registration.apply_plan(self.plan_path, plan_digest)

    def test_exact_external_success_is_byte_preserving_and_replay_safe(self) -> None:
        original_task = self.task.read_bytes()
        original_todo = self.todo.read_bytes()
        plan, plan_digest = self.prepared()

        receipt = registration.apply_plan(self.plan_path, plan_digest)
        first_receipt = receipt.read_bytes()
        replay = registration.apply_plan(self.plan_path, plan_digest)

        self.assertEqual(receipt, replay)
        self.assertEqual(first_receipt, replay.read_bytes())
        self.assertEqual(original_task, self.task.read_bytes())
        self.assertEqual(original_todo, self.todo.read_bytes())
        self.assertEqual(self.task, registration.resolve_registered_external_task(self.root, self.task_ref, self.task, self.registry))
        self.assertEqual(plan.key, registration.parse_receipt(first_receipt).key)

    def test_dry_run_only_emits_plan_and_does_not_mutate(self) -> None:
        before = self.tree_snapshot()
        with patch("builtins.print") as output:
            result = registration.main(
                [
                    "dry-run",
                    "--root",
                    str(self.root),
                    "--task",
                    str(self.task),
                    "--task-ref",
                    self.task_ref,
                    "--runat",
                    "mac_stt:3",
                    "--managerat",
                    "mac_stt:0",
                    "--task-sha256",
                    sha256(self.task),
                    "--todo-sha256",
                    sha256(self.todo),
                    "--registry",
                    str(self.registry),
                ]
            )
        self.assertEqual(0, result)
        self.assertEqual(before, self.tree_snapshot())
        self.assertEqual(registration.PLAN_SCHEMA, json.loads(output.call_args.args[0])["schema"])

    def test_prepare_rejects_task_todo_and_lifecycle_mismatch(self) -> None:
        with self.assertRaisesRegex(registration.RegistrationError, "expected digest"):
            registration.build_plan(self.root, self.task, self.task_ref, "mac_stt:3", "mac_stt:0", "0" * 64, sha256(self.todo), self.registry)
        with self.assertRaisesRegex(registration.RegistrationError, "expected digest"):
            registration.build_plan(self.root, self.task, self.task_ref, "mac_stt:3", "mac_stt:0", sha256(self.task), "0" * 64, self.registry)
        for replacement in (
            task_text(managerat="other:0"),
            task_text(runat="other:3"),
            task_text(status_value="running", blocked_on=""),
            task_text(blocked_on="persistent service"),
        ):
            self.task.write_text(replacement, encoding="utf-8")
            with self.assertRaisesRegex(registration.RegistrationError, "drifted"):
                self.plan()

    def test_prepare_rejects_membership_order_conflict_and_duplicate_owner(self) -> None:
        self.todo.write_text(f"current:\n- `{self.task_ref}` mac_stt:3\n- `{self.task_ref}` mac_stt:3\n", encoding="utf-8")
        with self.assertRaisesRegex(registration.RegistrationError, "exactly one"):
            self.plan()
        self.todo.write_text("current:\n- `../external/other.md` mac_stt:3\n", encoding="utf-8")
        with self.assertRaisesRegex(registration.RegistrationError, "exactly one"):
            self.plan()
        self.todo.write_text(f"current:\n- `{self.task_ref}` mac_stt:3\n", encoding="utf-8")
        owner = self.root / "duplicate.md"
        owner.write_text(task_text(), encoding="utf-8")
        owner.chmod(0o600)
        with self.assertRaisesRegex(registration.RegistrationError, "already owns"):
            self.plan()

    def test_prepare_rejects_prose_nonentry_wrong_target_and_previous_membership(self) -> None:
        invalid_todos = (
            f"current:\nA prose mention of `{self.task_ref}` for mac_stt:3.\n",
            f"current:\n- `{self.task_ref}` other:3\n",
            f"previous:\n- `{self.task_ref}` mac_stt:3\n",
        )
        for value in invalid_todos:
            with self.subTest(todo=value):
                self.todo.write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(registration.RegistrationError, "exact owner target"):
                    self.plan()

    def test_rejects_symlink_task_and_path_substitution(self) -> None:
        real = self.external / "real.md"
        self.task.rename(real)
        self.task.symlink_to(real)
        with self.assertRaises(OSError):
            self.plan()
        self.task.unlink()
        real.rename(self.task)
        plan, digest = self.prepared()
        replacement = self.external / "replacement.md"
        replacement.write_text(task_text(), encoding="utf-8")
        replacement.chmod(0o600)
        self.task.unlink()
        replacement.rename(self.task)
        with self.assertRaisesRegex(registration.RegistrationError, "identity changed"):
            registration.apply_plan(self.plan_path, digest)
        self.assertFalse((self.registry / f"registration-{plan.key}.json").exists())

    def test_apply_rejects_stale_task_todo_registry_and_plan_bytes(self) -> None:
        mutations = (
            lambda: self.task.write_text(task_text() + "changed\n", encoding="utf-8"),
            lambda: self.todo.write_text(self.todo.read_text(encoding="utf-8") + "\n", encoding="utf-8"),
            lambda: (self.registry / f"registration-{'f' * 64}.json").write_text("{}\n", encoding="utf-8"),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                with self.fresh_fixture_state():
                    plan, digest = self.prepared()
                    mutate()
                    with self.assertRaises(registration.RegistrationError):
                        registration.apply_plan(self.plan_path, digest)
                    self.assertFalse((self.registry / f"registration-{plan.key}.json").exists())
        with self.fresh_fixture_state():
            _plan, digest = self.prepared()
            self.plan_path.write_bytes(self.plan_path.read_bytes() + b" ")
            with self.assertRaisesRegex(registration.RegistrationError, "expected digest"):
                registration.apply_plan(self.plan_path, digest)

    def test_apply_rejects_recomputed_plan_bound_to_lookalike_todo(self) -> None:
        plan = self.plan()
        lookalike = self.external / "TODO.md"
        lookalike.write_bytes(self.todo.read_bytes())
        lookalike.chmod(0o600)
        forged = asdict(plan)
        forged["todo"] = str(lookalike)
        forged["key"] = hashlib.sha256(registration.canonical_bytes({key: value for key, value in forged.items() if key != "key"})).hexdigest()
        self.plan_path.write_bytes(registration.canonical_bytes(forged))
        self.plan_path.chmod(0o600)
        with self.assertRaisesRegex(registration.RegistrationError, "structure"):
            registration.apply_plan(self.plan_path, sha256(self.plan_path))
        self.assertEqual([], list(self.registry.glob("registration-*.json")))

    def test_conflicting_registration_is_rejected(self) -> None:
        _plan, _receipt = self.applied()
        with self.assertRaisesRegex(registration.RegistrationError, "conflicts"):
            self.plan()

    def test_same_task_or_target_cannot_be_registered_from_another_root(self) -> None:
        self.applied()
        second_root = Path(self.temporary.name) / "second-work-logs"
        second_root.mkdir(mode=0o700)
        second_todo = second_root / "TODO.md"
        second_todo.write_text(f"current:\n- `{self.task_ref}` mac_stt:3\n", encoding="utf-8")
        second_todo.chmod(0o600)
        with self.assertRaisesRegex(registration.RegistrationError, "conflicts"):
            registration.build_plan(second_root, self.task, self.task_ref, "mac_stt:3", "mac_stt:0", sha256(self.task), sha256(second_todo), self.registry)

        other_task = self.external / "other-task.md"
        other_task.write_text(task_text(), encoding="utf-8")
        other_task.chmod(0o600)
        other_ref = "../external/other-task.md"
        second_todo.write_text(f"current:\n- `{other_ref}` mac_stt:3\n", encoding="utf-8")
        with self.assertRaisesRegex(registration.RegistrationError, "conflicts"):
            registration.build_plan(second_root, other_task, other_ref, "mac_stt:3", "mac_stt:0", sha256(other_task), sha256(second_todo), self.registry)

        second_registry = Path(self.temporary.name) / "state" / "second-registry"
        second_registry.mkdir(mode=0o700)
        second_registry.chmod(0o700)
        with self.assertRaisesRegex(registration.RegistrationError, "authoritative ledger"):
            registration.build_plan(second_root, other_task, other_ref, "mac_stt:3", "mac_stt:0", sha256(other_task), sha256(second_todo), second_registry)

    def test_two_concurrent_applies_commit_exactly_one_registration(self) -> None:
        plan, digest = self.prepared()
        barrier = threading.Barrier(2)
        barrier_lock = threading.Lock()
        barrier_calls = 0
        original_current = registration.assert_plan_current
        outcomes: list[str] = []

        def synchronized_current(value: registration.RegistrationPlan):
            nonlocal barrier_calls
            with barrier_lock:
                barrier_calls += 1
                should_wait = barrier_calls <= 2
            if should_wait:
                barrier.wait(timeout=5)
            return original_current(value)

        def apply() -> None:
            try:
                outcomes.append(str(registration.apply_plan(self.plan_path, digest)))
            except Exception as exc:  # noqa: BLE001 - test records the competing result
                outcomes.append(type(exc).__name__)

        # Bypass the cooperative registry lock only at the revalidation seam to
        # exercise the atomic no-replace publication itself.
        with patch.object(registration, "registration_lock", side_effect=lambda _plan: registration.ExitStack()), patch.object(registration, "assert_plan_current", side_effect=synchronized_current):
            threads = [threading.Thread(target=apply) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        receipt = self.registry / f"registration-{plan.key}.json"
        self.assertTrue(receipt.is_file(), outcomes)
        self.assertEqual(1, sum(value == str(receipt) for value in outcomes))
        self.assertEqual(1, sum(value in {"FileExistsError", "RegistrationError"} for value in outcomes))

    def test_source_race_after_publication_is_invalidated_and_recoverable(self) -> None:
        plan, digest = self.prepared()
        original_publish = registration.publish_no_replace
        raced = False

        def publish_then_change(directory: Path, name: str, data: bytes) -> Path:
            nonlocal raced
            result = original_publish(directory, name, data)
            if name == f"registration-{plan.key}.json" and not raced:
                raced = True
                self.todo.write_bytes(self.todo.read_bytes() + b"\n")
            return result

        with patch.object(registration, "publish_no_replace", side_effect=publish_then_change):
            with self.assertRaisesRegex(registration.RegistrationError, "receipt was invalidated"):
                registration.apply_plan(self.plan_path, digest)

        receipt = self.registry / f"registration-{plan.key}.json"
        invalidation = self.registry / f"invalidation-{plan.key}.json"
        self.assertTrue(receipt.is_file())
        self.assertTrue(invalidation.is_file())
        self.assertEqual((), registration.active_receipts(self.registry))
        self.assertIsNone(registration.resolve_registered_external_task(self.root, self.task_ref, self.task, self.registry))
        replacement = registration.build_plan(
            self.root,
            self.task,
            self.task_ref,
            "mac_stt:3",
            "mac_stt:0",
            sha256(self.task),
            sha256(self.todo),
            self.registry,
        )
        self.assertNotEqual(plan.key, replacement.key)

    def test_rollback_is_append_only_replay_safe_and_fails_on_concurrent_state(self) -> None:
        _plan, receipt = self.applied()
        receipt_digest = sha256(receipt)
        rollback = registration.rollback_registration(receipt, receipt_digest)
        first = rollback.read_bytes()
        self.assertIsNone(registration.resolve_registered_external_task(self.root, self.task_ref, self.task, self.registry))
        self.assertEqual(rollback, registration.rollback_registration(receipt, receipt_digest))
        self.assertEqual(first, rollback.read_bytes())
        self.assertTrue(receipt.is_file())

        with self.fresh_fixture_state():
            _plan, receipt = self.applied()
            extra = self.registry / f"registration-{'e' * 64}.json"
            extra.write_text("{}\n", encoding="utf-8")
            extra.chmod(0o600)
            with self.assertRaisesRegex(registration.RegistrationError, "changed since"):
                registration.rollback_registration(receipt, sha256(receipt))
            self.assertFalse((self.registry / f"rollback-{registration.parse_receipt(receipt.read_bytes()).key}.json").exists())

    def test_tampered_receipt_and_rollback_never_activate_or_deactivate(self) -> None:
        plan, receipt = self.applied()
        values = json.loads(receipt.read_text(encoding="utf-8"))
        values["managerat"] = "attacker:0"
        receipt.write_text(json.dumps(values, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        with self.assertRaises(registration.RegistrationError):
            registration.active_receipts(self.registry)
        self.assertIsNone(registration.resolve_registered_external_task(self.root, self.task_ref, self.task, self.registry))

        receipt.write_bytes(registration.canonical_bytes(asdict(registration.receipt_for_plan(plan, sha256(self.plan_path)))))
        rollback = self.registry / f"rollback-{plan.key}.json"
        rollback.write_bytes(
            registration.canonical_bytes(
                {
                    "schema": registration.ROLLBACK_SCHEMA,
                    "key": plan.key,
                    "receipt": str(receipt),
                    "receipt_sha256": "0" * 64,
                }
            )
        )
        rollback.chmod(0o600)
        with self.assertRaisesRegex(registration.RegistrationError, "does not bind"):
            registration.active_receipts(self.registry)

    def test_cross_registry_receipt_copy_is_never_active(self) -> None:
        plan, receipt = self.applied()
        other_registry = Path(self.temporary.name) / "state" / "other-registry"
        other_registry.mkdir(mode=0o700)
        other_registry.chmod(0o700)
        copied = other_registry / receipt.name
        copied.write_bytes(receipt.read_bytes())
        copied.chmod(0o600)

        with self.assertRaisesRegex(registration.RegistrationError, "registry identity"):
            registration.active_receipts(other_registry)
        self.assertIsNone(registration.resolve_registered_external_task(self.root, self.task_ref, self.task, other_registry))

        registration.rollback_registration(receipt, sha256(receipt))
        self.assertTrue(copied.is_file())
        self.assertFalse((other_registry / f"rollback-{plan.key}.json").exists())
        self.assertIsNone(registration.resolve_registered_external_task(self.root, self.task_ref, self.task, other_registry))

    def test_disconnected_registry_append_invalidates_watcher_resolution(self) -> None:
        _plan, receipt = self.applied()
        forged = json.loads(receipt.read_text(encoding="utf-8"))
        forged["registry_sha256"] = "1" * 64
        plan_values = {key: value for key, value in forged.items() if key != "plan_sha256"}
        plan_values["schema"] = registration.PLAN_SCHEMA
        plan_values["key"] = hashlib.sha256(registration.canonical_bytes({key: value for key, value in plan_values.items() if key != "key"})).hexdigest()
        forged["key"] = plan_values["key"]
        forged["plan_sha256"] = hashlib.sha256(registration.canonical_bytes(plan_values)).hexdigest()
        disconnected = self.registry / f"registration-{forged['key']}.json"
        disconnected.write_bytes(registration.canonical_bytes(forged))
        disconnected.chmod(0o600)

        with self.assertRaisesRegex(registration.RegistrationError, "ambiguous or disconnected"):
            registration.active_receipts(self.registry)
        self.assertIsNone(registration.resolve_registered_external_task(self.root, self.task_ref, self.task, self.registry))

    def test_registry_scan_rejects_same_name_inode_replacement(self) -> None:
        _plan, receipt = self.applied()
        original_read = registration.read_file_at
        replaced = False

        def replace_after_first_read(parent_fd, parent_info, path, label, **kwargs):
            nonlocal replaced
            snapshot = original_read(parent_fd, parent_info, path, label, **kwargs)
            if label == "external task registry entry" and path.name == receipt.name and not replaced:
                replaced = True
                data = receipt.read_bytes()
                replacement = receipt.with_name("replacement.tmp")
                replacement.write_bytes(data)
                replacement.chmod(0o600)
                os.replace(replacement, receipt)
            return snapshot

        with patch.object(registration, "read_file_at", side_effect=replace_after_first_read):
            with self.assertRaisesRegex(registration.RegistrationError, "entry changed"):
                registration.registry_entries(self.registry)

    def test_symlinked_root_preserves_in_root_and_external_resolution(self) -> None:
        root_link = Path(self.temporary.name) / "linked-work-logs"
        root_link.symlink_to(self.root, target_is_directory=True)
        local = self.root / "local.md"
        local.write_text(task_text(runat="local:4"), encoding="utf-8")
        local.chmod(0o600)
        self.assertEqual(local, status.resolve_task_path(root_link, "local.md"))

        self.applied()
        with patch.dict(os.environ, {"OMO_EXTERNAL_TASK_REGISTRY": str(self.registry)}):
            self.assertEqual(self.task, status.resolve_task_path(root_link, self.task_ref))

    def test_watcher_discovers_registered_owner_and_does_not_report_tmux_unmanaged(self) -> None:
        args = status.Args(self.root, self.root / "sessions.json", False, False, True, "mac_stt:0", False)
        with patch.dict(os.environ, {"OMO_EXTERNAL_TASK_REGISTRY": str(self.registry)}):
            self.assertIsNone(status.resolve_task_path(self.root, self.task_ref))
            with (
                patch.object(status, "tmux_list_panes", return_value=["mac_stt:3"]),
                patch.object(status, "classify_target", return_value=status.StatusRow("tmux:mac_stt:3", "running", "", target="mac_stt:3")),
            ):
                before = status.tmux_unmanaged_problem_rows(args, set(), False, {}, "test")
            self.assertEqual(1, len(before))

            self.applied()
            self.assertEqual(self.task, status.resolve_task_path(self.root, self.task_ref))
            owners = status.active_task_targets(self.root, include_pending_delivery=True)
            self.assertEqual({"mac_stt:3"}, owners)
            with (
                patch.object(status, "tmux_list_panes", return_value=["mac_stt:3"]),
                patch.object(status, "classify_target", return_value=status.StatusRow("tmux:mac_stt:3", "running", "", target="mac_stt:3")) as classify,
            ):
                after = status.tmux_unmanaged_problem_rows(args, owners, False, {}, "test")
            self.assertEqual([], after)
            classify.assert_not_called()

    def test_helper_has_no_pane_mail_repository_or_product_side_effects(self) -> None:
        repository = Path(self.temporary.name) / "repository"
        repository.mkdir(mode=0o700)
        product = repository / "product.txt"
        product.write_text("unchanged\n", encoding="utf-8")
        before = self.tree_snapshot()
        product_before = product.read_bytes()
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            _plan, receipt = self.applied()
            registration.rollback_registration(receipt, sha256(receipt))
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(product_before, product.read_bytes())
        after = self.tree_snapshot()
        changed = set(before) ^ set(after) | {name for name in before.keys() & after.keys() if before[name] != after[name]}
        self.assertTrue(changed)
        self.assertTrue(all(str(self.registry.relative_to(Path(self.temporary.name))) in name or str(self.packet.relative_to(Path(self.temporary.name))) in name for name in changed))

    def tree_snapshot(self) -> dict[str, bytes]:
        base = Path(self.temporary.name)
        return {str(path.relative_to(base)): path.read_bytes() for path in base.rglob("*") if path.is_file() and not path.is_symlink()}

    @contextmanager
    def fresh_fixture_state(self):
        self.reset_fixture_state()
        try:
            yield
        finally:
            self.reset_fixture_state()

    def reset_fixture_state(self) -> None:
        for path in self.registry.glob("*"):
            path.unlink()
        for path in self.packet.glob("*"):
            path.unlink()
        self.task.write_bytes(task_text().encode())
        self.task.chmod(0o600)
        self.todo.write_text(f"current:\n- `{self.task_ref}` mac_stt:3\n", encoding="utf-8")
        self.todo.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
