from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from omo_manager import omo_agent_tree as tree
from omo_manager import omo_external_task_register as registration


def task_text(
    runat: str,
    managerat: str,
    *,
    status: str = "running",
    is_manager: bool = False,
    pending: tuple[str, ...] = (),
    blocked_on: str = "",
    purpose: str = "Carry out the assigned task.",
) -> str:
    blocked = f"blocked_on: {blocked_on}\n" if blocked_on else ""
    items = "pending_task_items: []\n" if not pending else "pending_task_items:\n" + "".join(f"  - {item}\n" for item in pending)
    return (
        "---\n"
        "version: v1.0.0\n"
        f"status: {status}\n"
        f"{blocked}"
        f"runat: {runat}\n"
        "tool: codex\n"
        f"managerat: {managerat}\n"
        f"is_manager: {'true' if is_manager else 'false'}\n"
        f"{items}"
        "---\n"
        "<manager_delegation from=\"top:0\">\n"
        f"{purpose}\n"
        "</manager_delegation>\n"
        "Task history.\n"
    )


class AgentTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.todo_rows: list[str] = []

    def add_task(self, name: str, text: str, *, indexed_target: str | None = None, section: str = "current") -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if indexed_target is not None:
            self.todo_rows.append(f"{section}:\n{name} {indexed_target}\n")

    def finish(self) -> None:
        (self.root / "TODO.md").write_text("".join(self.todo_rows) or "current:\n", encoding="utf-8")

    def args(
        self,
        *,
        agent: str | None = None,
        depth: int | None = None,
        statuses: tuple[str, ...] = tree.DEFAULT_STATUSES,
        json_output: bool = False,
    ) -> tree.Args:
        return tree.Args(self.root, agent, False, "top:0", depth, statuses, json_output)

    def basic_tree(self) -> None:
        self.add_task(
            "manager.md",
            task_text("team:1.0", "top:0", is_manager=True, pending=("Route build work",), purpose="Manage the build workers."),
            indexed_target="team:1",
        )
        self.add_task(
            "worker.md",
            task_text("team:2", "team:1.0", pending=("Fix the build",), purpose="Repair the failing build."),
            indexed_target="team:2.0",
        )
        self.finish()

    def test_text_tree_is_top_down_and_canonical(self) -> None:
        self.basic_tree()
        output = tree.run(self.args())
        self.assertLess(output.index("top:0 [main manager]"), output.index("team:1 [manager]"))
        self.assertIn("top:0 [main manager]\nwork: no open work recorded", output)
        self.assertLess(output.index("team:1 [manager]"), output.index("team:2 [worker]"))
        self.assertIn("task: manager.md [running]", output)
        self.assertIn("purpose: Manage the build workers.", output)
        self.assertIn("work: Fix the build", output)

    def test_depth_zero_prints_only_selected_root(self) -> None:
        self.basic_tree()
        output = tree.run(self.args(agent="team:1", depth=0))
        self.assertIn("team:1 [manager]", output)
        self.assertNotIn("team:2", output)
        self.assertNotIn("top:0", output)

    def test_duplicate_active_owner_records_fail(self) -> None:
        self.add_task("z.md", task_text("team:2", "top:0", pending=("second",)), indexed_target="team:2")
        self.add_task("a.md", task_text("team:2.0", "top:0"), indexed_target="team:2.0")
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "duplicate active owner records"):
            tree.run(self.args())

    def test_worker_may_parent_children_without_becoming_manager(self) -> None:
        self.add_task("parent.md", task_text("team:1", "top:0"), indexed_target="team:1")
        self.add_task("child.md", task_text("team:2", "team:1"), indexed_target="team:2")
        self.finish()
        output = tree.run(self.args())
        self.assertIn("team:1 [worker]", output)
        self.assertIn("team:2 [worker]", output)

    def test_json_contains_same_bounded_tree_and_task_fields(self) -> None:
        self.basic_tree()
        value = json.loads(tree.run(self.args(agent="team:1", depth=1, json_output=True)))
        self.assertEqual("team:1", value["selected_root"])
        self.assertEqual(["running", "long_running"], value["statuses"])
        self.assertEqual("team:2", value["tree"]["children"][0]["target"])
        self.assertEqual(["Route build work"], value["tree"]["current_work"])
        task = value["tree"]["tasks"][0]
        self.assertEqual("Manage the build workers.", task["purpose"])
        self.assertEqual(["Route build work"], task["pending_items"])
        self.assertEqual("todo:current", task["todo_membership"])
        self.assertFalse(task["external"])

    def test_unindexed_active_record_fails_before_output(self) -> None:
        self.add_task("worker.md", task_text("team:2", "top:0"))
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "missing its root TODO.md row"):
            tree.run(self.args())

    def test_malformed_indexed_task_records_fail(self) -> None:
        valid = task_text("team:2", "top:0")
        cases = {
            "version": valid.replace("version: v1.0.0\n", ""),
            "runat": valid.replace("runat: team:2\n", ""),
            "managerat": valid.replace("managerat: top:0\n", ""),
            "frontmatter": "Carry out the task.\n",
        }
        for field, source in cases.items():
            with self.subTest(field=field):
                self.add_task("worker.md", source, indexed_target="team:2")
                self.finish()
                with self.assertRaisesRegex(tree.TreeError, "invalid task frontmatter|no task frontmatter"):
                    tree.run(self.args())
                self.todo_rows.clear()

    def test_selected_task_requires_recorded_purpose(self) -> None:
        source = task_text("team:2", "top:0").replace(
            '<manager_delegation from="top:0">\nCarry out the assigned task.\n</manager_delegation>\n',
            "Task history only.\n",
        )
        self.add_task("worker.md", source, indexed_target="team:2")
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "no assignment paragraph"):
            tree.run(self.args())

    def test_duplicate_or_mismatched_index_fails(self) -> None:
        self.add_task("worker.md", task_text("team:2", "top:0"), indexed_target="team:2")
        self.todo_rows.append("current:\nworker.md team:2\n")
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "duplicate"):
            tree.run(self.args())

        self.todo_rows = ["current:\nworker.md team:9\n"]
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "does not match"):
            tree.run(self.args())

    def test_selected_blocked_record_may_be_unindexed_and_shows_blocker(self) -> None:
        self.add_task("worker.md", task_text("team:2", "top:0", status="blocked", blocked_on="human", pending=("Await decision",)))
        self.finish()
        output = tree.run(self.args(statuses=("blocked",)))
        self.assertIn("worker.md [blocked]", output)
        self.assertIn("blocked_on: human", output)

    def test_conflicting_parent_or_role_fails(self) -> None:
        self.add_task("a.md", task_text("team:2", "top:0"), indexed_target="team:2")
        self.add_task("b.md", task_text("team:2", "other:1", status="done"), indexed_target="team:2")
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "conflicting managers"):
            tree.run(self.args(statuses=("running", "done")))

        (self.root / "b.md").write_text(task_text("team:2", "top:0", status="done", is_manager=True), encoding="utf-8")
        with self.assertRaisesRegex(tree.TreeError, "conflicting manager roles"):
            tree.run(self.args(statuses=("running", "done")))

    def test_missing_parent_and_cycle_fail(self) -> None:
        self.add_task("worker.md", task_text("team:2", "missing:1"), indexed_target="team:2")
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "missing parent"):
            tree.run(self.args())

        (self.root / "worker.md").write_text(task_text("team:3", "top:0", status="done"), encoding="utf-8")
        self.todo_rows.clear()
        self.add_task("a.md", task_text("team:1", "team:2", is_manager=True), indexed_target="team:1")
        self.add_task("b.md", task_text("team:2", "team:1", is_manager=True), indexed_target="team:2")
        self.finish()
        with self.assertRaisesRegex(tree.TreeError, "cycle"):
            tree.run(self.args())

    def test_omnigent_explicit_root_is_supported(self) -> None:
        target = "omnigent://session_1"
        self.add_task("worker.md", task_text(target, "top:0"), indexed_target=target)
        self.finish()
        self.assertIn(target, tree.run(self.args(agent=target)))

    def test_default_root_is_current_agent_when_tmux_resolves(self) -> None:
        self.basic_tree()
        with patch("omo_manager.omo_agent_tree.current_tmux_target", return_value="team:1"):
            args = tree.Args(self.root, None, False, "top:0", None, tree.DEFAULT_STATUSES, False)
            output = tree.run(args)
        self.assertTrue(output.startswith("team:1 [manager]"))
        self.assertNotIn("top:0", output)

    def test_full_tree_ignores_current_agent(self) -> None:
        self.basic_tree()
        with patch("omo_manager.omo_agent_tree.current_tmux_target", return_value="team:1"):
            args = tree.Args(self.root, None, True, "top:0", None, tree.DEFAULT_STATUSES, False)
            output = tree.run(args)
        self.assertTrue(output.startswith("top:0 [main manager]"))

    def test_local_environment_is_strict_and_process_values_win(self) -> None:
        local_env = self.root / "local.env"
        local_env.write_text(f"OMO_WORK_LOGS_ROOT={self.root}\nOMO_MANAGER_TMUX_TARGET=top:0\n", encoding="utf-8")
        self.finish()
        clean = {key: value for key, value in os.environ.items() if key not in {"OMO_WORK_LOGS_ROOT", "OMO_MANAGER_TMUX_TARGET"}}
        clean["OMO_MANAGER_LOCAL_ENV"] = str(local_env)
        with patch.dict(os.environ, clean, clear=True):
            configured = tree.configuration(tree.Args(None, None, False, None, None, tree.DEFAULT_STATUSES, False))
        self.assertEqual(self.root, configured.root)
        self.assertEqual("top:0", configured.main_manager)

        local_env.write_text(f"OMO_WORK_LOGS_ROOT={self.root}\nOMO_MANAGER_TMUX_TARGET=top:0\nOMO_MANAGER_TMUX_TARGET=top:1\n", encoding="utf-8")
        with patch.dict(os.environ, clean, clear=True), self.assertRaisesRegex(tree.TreeError, "more than once"):
            tree.configuration(tree.Args(None, None, False, None, None, tree.DEFAULT_STATUSES, False))

    def test_registered_external_record_uses_source_todo(self) -> None:
        source = self.root.parent / f"{self.root.name}-external"
        registry = self.root.parent / f"{self.root.name}-registry"
        packet = self.root.parent / f"{self.root.name}-packet"
        for directory in (source, registry, packet):
            directory.mkdir(mode=0o700)
            self.addCleanup(lambda path=directory: __import__("shutil").rmtree(path, ignore_errors=True))
        external_task = source / "external.md"
        external_task.write_text(task_text("ext:1", "top:0", status="blocked", blocked_on="human", pending=("Await hardware",)), encoding="utf-8")
        source_todo = source / "TODO.md"
        source_todo.write_text("human pending:\nexternal.md ext:1\n", encoding="utf-8")
        self.finish()

        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        with patch("omo_manager.omo_external_task_register.default_registry_dir", return_value=registry):
            plan = registration.build_plan(
                self.root,
                source,
                external_task,
                "external.md",
                "ext:1",
                "top:0",
                digest(external_task),
                digest(source_todo),
                registry,
            )
            plan_path = packet / "plan.json"
            registration.write_plan(plan_path, plan)
            registration.apply_plan(plan_path, digest(plan_path))
            with patch("omo_manager.omo_agent_tree.default_registry_dir", return_value=registry):
                records = tree.external_records(self.root, ("blocked",))
        self.assertEqual(1, len(records))
        self.assertTrue(records[0].external)
        self.assertIn("todo:human pending", records[0].todo_membership or "")

    def test_help_is_the_complete_usage_reference(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            tree.parse_args(["--help"])
        self.assertEqual(0, raised.exception.code)
        help_text = output.getvalue()
        for required in (
            "read-only",
            "managerat -> runat",
            "current tmux pane",
            "--full-tree",
            "--main-manager",
            "-L N",
            "--all-statuses",
            "omnigent://",
            "TODO.md",
            "JSON",
            "Exit status",
            "Examples",
        ):
            self.assertIn(required, help_text)

    def test_cli_errors_use_distinct_exit_statuses(self) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as malformed:
                tree.parse_args(["--depth", "-1"])
        self.assertEqual(2, malformed.exception.code)

        error = StringIO()
        with redirect_stderr(error):
            result = tree.main(["--root", str(self.root / "missing"), "--main-manager", "top:0"])
        self.assertEqual(tree.EXIT_MISSING_ROOT, result)
        self.assertIn("does not exist", error.getvalue())

        self.finish()
        with patch.dict(
            os.environ,
            {"OMO_WORK_LOGS_ROOT": str(self.root), "OMO_MANAGER_TMUX_TARGET": ""},
            clear=True,
        ), redirect_stderr(StringIO()):
            self.assertEqual(tree.EXIT_CONFIGURATION, tree.main([]))

        self.add_task("worker.md", task_text("team:2", "missing:1"), indexed_target="team:2")
        self.finish()
        with redirect_stderr(StringIO()):
            self.assertEqual(
                tree.EXIT_INVALID_STATE,
                tree.main(["--root", str(self.root), "--main-manager", "top:0"]),
            )

        (self.root / "worker.md").unlink()
        self.todo_rows.clear()
        self.finish()
        with redirect_stderr(StringIO()):
            self.assertEqual(
                tree.EXIT_MISSING_ROOT,
                tree.main(["--root", str(self.root), "--main-manager", "top:0", "--agent", "team:9"]),
            )


if __name__ == "__main__":
    unittest.main()
