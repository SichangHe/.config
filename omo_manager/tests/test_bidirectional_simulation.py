from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_pending_watch as watcher
from omo_manager.omo_blocking import BlockingError
from omo_manager.omo_blocking import acknowledge
from omo_manager.omo_blocking import add_dependency
from omo_manager.omo_blocking import load_task
from omo_manager.omo_blocking import reconcile
from omo_manager.omo_blocking import render_task
from omo_manager.omo_blocking import resolve_item
from omo_manager.omo_blocking import v2_enabled
from omo_manager.omo_blocking_actor import BlockingActor
from omo_manager.omo_pending import Args as PendingArgs
from omo_manager.omo_pending import run as run_pending
from omo_manager.omo_task_migrate import Args as MigrationArgs
from omo_manager.omo_task_migrate import run as run_migration
from omo_manager.omo_task_metadata import TaskMetadata
from omo_manager.omo_task_metadata import parse_task_metadata


def v1_task(runat: str, item: str, body: str) -> str:
    return f"""---
version: v1.0.0
status: running
runat: {runat}
tool: codex
managerat: mgr:1
is_manager: false
pending_task_items:
  - {item}
---
{body}
"""


def commit(root: Path, *paths: str) -> None:
    if not (root / ".git").exists():
        template = root / "git-template"
        template.mkdir()
        _ = subprocess.run(["git", "init", "-q", f"--template={template}"], cwd=root, check=True)
    _ = subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
    _ = subprocess.run(
        ["git", "-c", "user.name=Simulation", "-c", "user.email=simulation@example.invalid", "commit", "-q", "-m", "simulation state"],
        cwd=root,
        check=True,
    )


def metadata(path: Path, root: Path) -> TaskMetadata:
    value = parse_task_metadata(path.read_text(encoding="utf-8"), root)
    if value is None:
        raise AssertionError(f"simulation task lacks metadata: {path.name}")
    return value


def restore_environment(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            _ = os.environ.pop(key, None)
        else:
            os.environ[key] = value


class BidirectionalCompatibilitySimulation(unittest.TestCase):
    def test_current_v1_setup_migrates_without_changing_human_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            isolated_environment = {
                "GIT_CONFIG_GLOBAL": str(root / "gitconfig"),
                "GIT_CONFIG_COUNT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "XDG_STATE_HOME": str(root / "state"),
            }
            previous_environment = {key: os.environ.get(key) for key in isolated_environment}
            os.environ.update(isolated_environment)
            self.addCleanup(restore_environment, previous_environment)
            source_path = root / "source.md"
            owner_path = root / "owner.md"
            plan_path = root / "migration.yaml"
            _ = source_path.write_text(v1_task("src:2", "produce reviewed result", "source body"), encoding="utf-8")
            _ = owner_path.write_text(v1_task("own:2", "integrate reviewed result", "owner body"), encoding="utf-8")
            _ = (root / "TODO.md").write_text("current:\nsource.md src:2\nowner.md own:2\n", encoding="utf-8")
            commit(root, "TODO.md", "source.md", "owner.md")

            legacy_output = StringIO()
            with patch("omo_manager.omo_pending.current_active_task", return_value=owner_path), redirect_stdout(legacy_output):
                self.assertEqual(0, run_pending(PendingArgs("add", ("temporary legacy work",)), root))
                self.assertEqual(0, run_pending(PendingArgs("list"), root))
                self.assertEqual(
                    0,
                    run_pending(PendingArgs("remove", ("temporary legacy work",), evidence="simulation completed"), root),
                )
            self.assertIn("temporary legacy work", legacy_output.getvalue())
            self.assertNotIn(owner_path.name, legacy_output.getvalue())
            legacy_text = owner_path.read_text(encoding="utf-8")
            self.assertIn("  - integrate reviewed result\n", legacy_text)
            self.assertNotIn("  - temporary legacy work\n", legacy_text)
            self.assertIn("verified removed pending item: simulation completed", legacy_text)
            commit(root, "owner.md")

            self.assertEqual(0, run_migration(MigrationArgs(root, "plan", plan_path)))
            self.assertFalse(v2_enabled(root))
            with self.assertRaisesRegex(BlockingError, "migration plan must be clean and committed"):
                _ = run_migration(MigrationArgs(root, "dry-run", plan_path))
            commit(root, "migration.yaml")
            self.assertEqual(0, run_migration(MigrationArgs(root, "dry-run", plan_path)))
            reviewed_owner = owner_path.read_text(encoding="utf-8")
            _ = owner_path.write_text(f"{reviewed_owner}unreviewed drift\n", encoding="utf-8")
            with self.assertRaisesRegex(BlockingError, "drifted from the reviewed migration plan"):
                _ = run_migration(MigrationArgs(root, "write", plan_path))
            _ = owner_path.write_text(reviewed_owner, encoding="utf-8")
            self.assertEqual(0, run_migration(MigrationArgs(root, "write", plan_path)))
            commit(root, "source.md", "owner.md")
            self.assertFalse(v2_enabled(root))
            self.assertEqual(0, run_migration(MigrationArgs(root, "enable", plan_path)))
            self.assertTrue(v2_enabled(root))

            source_item = metadata(source_path, root).pending_items[0]
            owner_item = metadata(owner_path, root).pending_items[0]
            add_dependency(root, owner_path, owner_item.id, source_path, source_item.id)
            resolve_item(load_task(source_path, root=root), source_item.id, "completed", "simulation dependency complete")
            self.assertFalse(reconcile(root).errors)

            current_source = load_task(source_path, root=root)
            source_body = f"{current_source.body.rstrip()}\n(pending)\nKeep ordinary human delivery direct\n"
            _ = source_path.write_text(render_task(current_source.metadata, source_body), encoding="utf-8")
            actor = BlockingActor(root)
            actor.start()
            args = watcher.Args(
                root,
                "",
                root / "seen.tsv",
                1,
                300,
                1800,
                Path("/bin/false"),
                True,
                True,
                manager_target="mgr:1",
            )
            delivery_output = StringIO()
            deliveries: list[tuple[str, str]] = []

            def capture_delivery(
                _args: watcher.Args,
                _marker: watcher.Marker,
                text: str,
                target: str,
                _success_event: watcher.DeliverySuccessEvent | None = None,
                **_options: object,
            ) -> watcher.DeliveryResult:
                deliveries.append((target, text))
                print(text)
                return watcher.DeliveryResult(0)

            try:
                with redirect_stdout(delivery_output), patch.object(watcher, "push_marker_delivery", side_effect=capture_delivery):
                    self.assertTrue(watcher.scan_once(args, {}, [source_path, owner_path]))
            finally:
                actor.close()

            delivered = delivery_output.getvalue()
            self.assertEqual(2, len(deliveries))
            delivery_by_target = dict(deliveries)
            self.assertEqual({"src:2", "own:2"}, set(delivery_by_target))
            self.assertIn("<human_instruction>", delivery_by_target["src:2"])
            self.assertIn("Keep ordinary human delivery direct", delivery_by_target["src:2"])
            self.assertIn("Pending item ready:", delivery_by_target["own:2"])
            self.assertNotIn("<human_instruction>", delivery_by_target["own:2"])
            self.assertIn("<human_instruction>", delivered)
            self.assertIn("Keep ordinary human delivery direct", delivered)
            self.assertIn("Pending item ready:", delivered)
            self.assertIn("omo_pending.py wake-ack --notice-id", delivered)
            self.assertEqual(1, delivered.count("<human_instruction>"))
            queued_owner = load_task(owner_path, root=root)
            notice = metadata(owner_path, root).pending_items[0].notices[-1]
            self.assertEqual(("pending", 1, "own:2"), (notice.state, notice.attempt_count, notice.target_snapshot))

            _ = acknowledge(queued_owner, notice.id)
            self.assertNotIn(notice.id, load_task(owner_path, root=root).body)
            self.assertIn("Keep ordinary human delivery direct", load_task(source_path, root=root).body)


if __name__ == "__main__":
    _ = unittest.main()
