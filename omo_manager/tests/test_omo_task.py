import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_manager_rotate import ProcessInfo
from omo_manager.omo_task import (
    CODEX_LAUNCH_STARTED,
    CODEX_LAUNCH_UPDATED,
    Args,
    DEFAULT_TOOL,
    DEFAULT_WORKER_INSTRUCTIONS,
    LaunchSession,
    PCODX_WRAPPER,
    PENDING_TASK_ITEMS_MARKER,
    VL_WORKER_INSTRUCTIONS,
    codex_cmd,
    effective_tool,
    ensure_task_file,
    has_live_codex_launch,
    refreshed_todo_entry,
    is_vl_agent,
    launched_frontmatter_text,
    link_todo,
    main,
    new_window,
    parse_args,
    prompt_input,
    runat_goal_tree_error,
    runat_header_error,
    start_codex,
    validate_inputs,
    validate_existing_target_runtime,
    validate_runat_goal_tree,
    wait_command_started,
    wait_shell,
    write_human_instruction_file,
)
from omo_manager.tests.test_task_metadata_v2 import v2_task


VALID_GOAL_TREE = "implement manager check\n- reject missing task goal tree\n"


def trust_screen(launch_marker: str) -> list[str]:
    return [
        launch_marker,
        "> You are in /workspace/project",
        "",
        "  Do you trust the contents of this directory? Working with untrusted",
        "  contents comes with higher risk of prompt injection. Trusting the",
        "  directory allows project-local config, hooks, and exec policies to",
        "  load.",
        "",
        "› 1. Yes, continue",
        "  2. No, quit",
        "",
        "  Press enter to continue",
    ]


CAPTURED_TRUST_POPUP = """> You are in /ssd1/sichangheagent/vlnfix1

  Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection. Trusting the directory allows project-local config, hooks, and exec policies to
  load.

› 1. Yes, continue
  2. No, quit

  Press enter to continue
""".splitlines() + [""] * 54


class OmoTaskTests(unittest.TestCase):
    def render_prompt(self, expression: str) -> bytes:
        result = subprocess.run(
            ["bash", "-c", f'set -- {expression}; printf %s "$1"'],
            capture_output=True,
            check=True,
        )
        return result.stdout

    def test_creates_task_file_with_runat_header_and_todo_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, 'x.md', 'cfg', '2', 'codex', None, '', prompt, False, False, '', '', (), False, "mgr:1")
            path = ensure_task_file(args, 'cfg:2')
            link_todo(args, 'cfg:2')
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("running", metadata.status)
            self.assertEqual("cfg:2", metadata.runat)
            self.assertEqual("codex", metadata.tool)
            self.assertEqual("mgr:1", metadata.managerat)
            self.assertFalse(metadata.is_manager)
            self.assertEqual((), metadata.pending_task_items)
            self.assertNotIn(PENDING_TASK_ITEMS_MARKER, path.read_text(encoding="utf-8"))
            self.assertIn('x.md cfg:2', (root / 'TODO.md').read_text(encoding='utf-8'))

    def test_absolute_task_file_writes_relative_todo_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "nested" / "x.md"
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, str(task), "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "mgr:1")
            _ = ensure_task_file(args, "cfg:2")
            link_todo(args, "cfg:2")
            text = (root / "TODO.md").read_text(encoding="utf-8")
            self.assertIn("nested/x.md cfg:2", text)
            self.assertNotIn(str(root), text)

    def test_link_todo_normalizes_existing_absolute_task_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "x.md"
            _ = (root / "TODO.md").write_text(f"current:\n{task} cfg:1\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, str(task), "cfg", "1", "codex", None, "", prompt, False, False, "", "", ())
            link_todo(args, "cfg:1")
            self.assertEqual("current:\nx.md cfg:1\n", (root / "TODO.md").read_text(encoding="utf-8"))

    def test_link_todo_normalizes_absolute_entry_when_called_with_relative_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "x.md"
            _ = (root / "TODO.md").write_text(f"current:\n{task} cfg:1\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "1", "codex", None, "", prompt, False, False, "", "", ())
            link_todo(args, "cfg:1")
            self.assertEqual("current:\nx.md cfg:1\n", (root / "TODO.md").read_text(encoding="utf-8"))

    def test_link_todo_updates_existing_task_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / "TODO.md").write_text("current:\nx.md cfg:1\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", None, False, False, "", "", ())
            link_todo(args, "cfg:2")
            self.assertEqual("current:\nx.md cfg:2\n", (root / "TODO.md").read_text(encoding="utf-8"))

    def test_refreshed_todo_entry_preserves_annotations(self) -> None:
        self.assertEqual("  x.md cfg:2 (blocked: old note)", refreshed_todo_entry("  /tmp/root/x.md cfg:1 (blocked: old note)", "x.md", "cfg:2"))
        self.assertEqual("x.md cfg:2; followup.md", refreshed_todo_entry("x.md cfg:1; followup.md", "x.md", "cfg:2"))
        self.assertEqual("x.md cfg:2 (running)", refreshed_todo_entry("x.md cfg 1 (running)", "x.md", "cfg:2"))

    def test_creates_pcodx_task_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, 'x.md', 'cfg', '2', 'pcodx', None, '', prompt, False, False, '', '', (), False, "mgr:1")
            path = ensure_task_file(args, 'cfg:2')
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("pcodx", metadata.tool)

    def test_validates_runat_goal_tree(self) -> None:
        validate_runat_goal_tree("runat: cfg:2 pcodx\nimplement manager check\n- reject missing task goal tree\n")
        validate_runat_goal_tree("runat: cfg:2 pcodx\nmanagerat: wl:1\nimplement manager check\n- reject missing task goal tree\n")
        self.assertIn("high-level goal", runat_goal_tree_error("runat: cfg:2 pcodx\n\n- reject missing task goal tree\n"))
        self.assertIn("plain high-level", runat_goal_tree_error("runat: cfg:2 pcodx\n- implement manager check\n- reject missing task goal tree\n"))
        self.assertIn("concrete bullet subgoal", runat_goal_tree_error("runat: cfg:2 pcodx\nimplement manager check\n"))
        self.assertIn("concrete bullet subgoal", runat_goal_tree_error("runat:\tcfg:2 pcodx\nimplement manager check\n"))

    def test_new_frontmatter_task_accepts_blank_before_bullet_subgoals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "implement manager check\n\n- reproduce the failure\n- repair validation\n- verify dispatch\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "cfg",
                        "--tmux-window",
                        "2",
                        "--prompt-file",
                        str(prompt),
                        "--manager-target",
                        "mgr:1",
                        "--no-link",
                        "--dry-run",
                    ]
                )
            self.assertEqual(0, result, stdout.getvalue())

    def test_rejects_collapsed_runat_header(self) -> None:
        text = (
            "runat: vl:13 codex managerat: vl:15 Guide the human step by step through\n"
            "failing VL experiments. (above are pending task items)\n"
        )
        self.assertIn("exactly `runat: TARGET TOOL`", runat_header_error(text))

    def test_existing_task_rejects_collapsed_header_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            path = root / "vl_worker.md"
            original = (
                "runat: vl:1 codex managerat: vl:15 collapsed goal\n"
                "work\n"
                "- route\n"
                "(above are pending task items)\n"
            )
            path.write_text(original, encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "medium", (), False, "vl:15", model="gpt-5.6-terra")
            with self.assertRaisesRegex(ValueError, "exactly `runat: TARGET TOOL`"):
                validate_inputs(args)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_existing_one_line_task_inserts_managerat_on_new_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vl_worker.md"
            path.write_text("runat: vl:1 codex", encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", None, "", None, False, False, "", "", (), False, "vl:15")
            validate_inputs(args)
            ensure_task_file(args, "vl:1")
            self.assertEqual("runat: vl:1 codex\nmanagerat: vl:15\n", path.read_text(encoding="utf-8"))

    def test_existing_task_rejects_malformed_managerat_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "vl_worker.md"
            original = (
                "runat: vl:1 codex\n"
                "managerat: vl:15 Guide the human\n"
                "work\n"
                "- route\n"
            )
            path.write_text(original, encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", None, "", None, False, False, "", "", (), False, "vl:15")
            with self.assertRaisesRegex(ValueError, "managerat: TARGET"):
                validate_inputs(args)
            self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_new_task_preserves_prompt_started_malformed_managerat_as_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "managerat: vl:15 extra\n"
                "Goal\n"
                "- item\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "vl:15")
            validate_inputs(args)
            path = ensure_task_file(args, "cfg:2")
            self.assertIn("managerat: vl:15 extra\nGoal\n- item\n", path.read_text(encoding="utf-8"))

    def test_new_task_frontmatter_carries_manager_target_when_prompt_starts_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "managerat: vl:15 extra\n"
                "Goal\n"
                "- item\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "vl:15")
            path = ensure_task_file(args, "cfg:2")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("vl:15", metadata.managerat)

    def test_new_task_preserves_prompt_started_no_space_managerat_as_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "managerat:vl:15 extra\n"
                "Goal\n"
                "- item\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "vl:15")
            path = ensure_task_file(args, "cfg:2")
            self.assertIn("managerat:vl:15 extra\nGoal\n- item\n", path.read_text(encoding="utf-8"))

    def test_new_task_preserves_prompt_started_collapsed_header_as_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "runat: cfg:2 codex managerat: wl:1 collapsed\n"
                "Goal\n"
                "- item\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")
            path = ensure_task_file(args, "cfg:2")
            self.assertIn("runat: cfg:2 codex managerat: wl:1 collapsed\nGoal\n- item\n", path.read_text(encoding="utf-8"))

    def test_existing_tabbed_runat_header_is_replaced_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text("runat:\tcfg:2 codex\nold goal\n- item\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "pcodx", root, "", None, False, False, "", "medium", (), model="gpt-5.6-terra")
            validate_inputs(args)
            ensure_task_file(args, "cfg:2")
            self.assertEqual("runat: cfg:2 pcodx\nold goal\n- item\n", path.read_text(encoding="utf-8"))

    def test_new_task_file_writes_managerat_before_pending_items_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")
            path = ensure_task_file(args, "cfg:2")
            self.assertEqual(
                "---\n"
                "version: v1.0.0\n"
                "status: running\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: wl:1\n"
                "is_manager: false\n"
                "pending_task_items: []\n"
                "---\n"
                "implement manager check\n"
                "- reject missing task goal tree\n",
                path.read_text(encoding="utf-8"),
            )

    def test_new_manager_task_starts_long_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "manager.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), manager_target="wl:1", is_manager=True)

            metadata = parse_task_metadata(ensure_task_file(args, "cfg:2").read_text(encoding="utf-8"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("persistent manager role", metadata.blocked_on)
            self.assertTrue(metadata.is_manager)

    def test_manager_relaunch_clears_blocker_for_long_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = (
                "---\n"
                "version: v1.0.0\n"
                "status: blocked\n"
                "blocked_on: waiting for a manager decision\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: wl:1\n"
                "is_manager: true\n"
                "pending_task_items: []\n"
                "---\n"
                "continue coordination\n"
            )
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            updated = launched_frontmatter_text(existing, args, "cfg:2")
            metadata = parse_task_metadata(updated)

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("", metadata.blocked_on)

    def test_manager_relaunch_preserves_long_running_without_blocked_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = (
                "---\n"
                "version: v1.0.0\n"
                "status: long_running\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: wl:1\n"
                "is_manager: true\n"
                "pending_task_items: []\n"
                "---\n"
                "continue coordination\n"
            )
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            metadata = parse_task_metadata(launched_frontmatter_text(existing, args, "cfg:2"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("", metadata.blocked_on)

    def test_manager_relaunch_preserves_missing_frontmatter_blocked_on_when_body_mentions_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = (
                "---\nversion: v1.0.0\nstatus: long_running\nrunat: cfg:2\ntool: codex\n"
                "managerat: wl:1\nis_manager: true\npending_task_items:\n  - [ ] review migration\n---\n"
                "A body example may mention blocked_on: without defining the field.\n"
            )
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            metadata = parse_task_metadata(launched_frontmatter_text(existing, args, "cfg:2"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("", metadata.blocked_on)
            self.assertEqual(("[ ] review migration",), metadata.pending_task_items)

    def test_manager_relaunch_preserves_custom_persistent_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = (
                "---\nversion: v1.0.0\nstatus: long_running\nblocked_on: persistent human-facing audit contact\n"
                "runat: cfg:2\ntool: codex\nmanagerat: wl:1\nis_manager: true\npending_task_items: []\n---\nbody\n"
            )
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            metadata = parse_task_metadata(launched_frontmatter_text(existing, args, "cfg:2"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("persistent human-facing audit contact", metadata.blocked_on)

    def test_worker_relaunch_preserves_long_running_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = (
                "---\nversion: v1.0.0\nstatus: long_running\nblocked_on: persistent human-facing audit contact\n"
                "runat: cfg:2\ntool: codex\nmanagerat: wl:1\nis_manager: false\npending_task_items: []\n---\nbody\n"
            )
            args = Args(root, "worker.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", model="gpt-5.6-terra")

            metadata = parse_task_metadata(launched_frontmatter_text(existing, args, "cfg:2"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("persistent human-facing audit contact", metadata.blocked_on)

    def test_manager_relaunch_validation_and_write_preserves_blockerless_long_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "manager.md"
            task.write_text(
                "---\nversion: v1.0.0\nstatus: long_running\nrunat: cfg:2\ntool: codex\nmanagerat: wl:1\nis_manager: true\npending_task_items: []\n---\ncontinue coordination\n",
                encoding="utf-8",
            )
            (root / "MANAGER.md").write_text("manager instructions\n", encoding="utf-8")
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            self.assertEqual("", validate_inputs(args))
            metadata = parse_task_metadata(ensure_task_file(args, "cfg:2").read_text(encoding="utf-8"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("", metadata.blocked_on)

    def test_v2_manager_relaunch_preserves_long_running_without_blocked_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = (
                "---\n"
                "version: v2.0.0\n"
                "task_id: task_019f0000-0000-7000-8000-000000000001\n"
                "status: long_running\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: wl:1\n"
                "is_manager: true\n"
                "pending_task_items: []\n"
                "resolved_task_items: []\n"
                "---\n"
                "continue coordination\n"
            )
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            metadata = parse_task_metadata(launched_frontmatter_text(existing, args, "cfg:2"))

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("", metadata.blocked_on)

    def test_v2_manager_relaunch_preserves_persistent_reason_with_generated_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = v2_task().replace("resume_status: running", "resume_status: long_running").replace("is_manager: false", "is_manager: true").replace(
                "  - kind: human\n    reason: waiting for approval",
                "  - kind: persistent\n    reason: persistent specialized manager role",
            )
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            updated = launched_frontmatter_text(existing, args, "cfg:2")

            self.assertIn("reason: persistent specialized manager role", updated)
            self.assertNotIn("reason: persistent manager role", updated)

    def test_v2_worker_relaunch_preserves_long_running_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = v2_task().replace("status: blocked\nresume_status: running", "status: long_running").replace(
                "  - kind: pending_items\n    item_ids: [pi_019f0000-0000-7000-8000-000000000003]\n  - kind: human\n    reason: waiting for approval\n",
                "  - kind: persistent\n    reason: persistent human-facing audit contact\n",
            )
            args = Args(root, "worker.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", model="gpt-5.6-terra")

            metadata = parse_task_metadata(launched_frontmatter_text(existing, args, "cfg:2"), root)

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("long_running", metadata.status)
            self.assertEqual("persistent human-facing audit contact", metadata.blocked_on)

    def test_v2_manager_relaunch_preserves_external_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = v2_task().replace("resume_status: running", "resume_status: long_running").replace("is_manager: false", "is_manager: true")
            args = Args(root, "manager.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), manager_target="wl:1", is_manager=True, model="gpt-5.6-terra")

            updated = launched_frontmatter_text(existing, args, "cfg:2")
            metadata = parse_task_metadata(updated, root)

            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("blocked", metadata.status)
            self.assertEqual("long_running", metadata.resume_status)
            self.assertIn("waiting for approval", metadata.blocked_on)
            self.assertNotIn("persistent manager role", metadata.blocked_on)

    def test_vl_worker_launch_requires_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "medium", (), model="gpt-5.6-terra")
            with self.assertRaisesRegex(ValueError, "require --manager-target"):
                validate_inputs(args)

    def test_vl_submanager_launch_does_not_require_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "vl_submanager_current_8653.md", "vl", "15", "codex", root, "", prompt, False, False, "", "medium", (), model="gpt-5.6-terra")
            with patch.dict("os.environ", {"OMO_AGENT_TMUX_TARGET": "main:1"}):
                validate_inputs(args)

    def test_existing_vl_worker_launch_writes_missing_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            path = root / "vl_worker.md"
            path.write_text("runat: vl:1 codex\nwork\n- route\n(above are pending task items)\n", encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "medium", (), False, "vl:15", model="gpt-5.6-terra")
            validate_inputs(args)
            ensure_task_file(args, "vl:1")
            self.assertIn("managerat: vl:15\n", path.read_text(encoding="utf-8"))

    def test_existing_vl_worker_launch_rejects_conflicting_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            path = root / "vl_worker.md"
            path.write_text("runat: vl:1 codex\nmanagerat: vl:14\nwork\n- route\n(above are pending task items)\n", encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "medium", (), False, "vl:15", model="gpt-5.6-terra")
            with self.assertRaisesRegex(ValueError, "does not match --manager-target"):
                validate_inputs(args)

    def test_prompt_mentioning_pending_marker_still_gets_marker_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "review task boilerplate\n"
                "- check whether any created task is missing `(above are pending task items)`\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")
            path = ensure_task_file(args, "cfg:2")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(0, lines.count(PENDING_TASK_ITEMS_MARKER))
            self.assertEqual("- check whether any created task is missing `(above are pending task items)`", lines[-1])

    def test_prompt_containing_exact_pending_marker_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "review task boilerplate\n"
                "- check marker handling\n"
                "(above are pending task items)\n"
                "- keep this human item pending\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")
            path = ensure_task_file(args, "cfg:2")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, lines.count(PENDING_TASK_ITEMS_MARKER))
            self.assertEqual("- keep this human item pending", lines[-1])

    def test_new_task_file_keeps_prompt_body_after_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "route cleanup\n"
                "- preserve human wording\n"
                "\n"
                "Human sources:\n"
                "\n"
                "- manager_mail/8649.txt\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")
            path = ensure_task_file(args, "cfg:2")
            self.assertEqual(
                "---\n"
                "version: v1.0.0\n"
                "status: running\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: wl:1\n"
                "is_manager: false\n"
                "pending_task_items: []\n"
                "---\n"
                "route cleanup\n"
                "- preserve human wording\n"
                "\n"
                "Human sources:\n"
                "\n"
                "- manager_mail/8649.txt\n",
                path.read_text(encoding="utf-8"),
            )

    def test_existing_frontmatter_task_appends_prompt_without_legacy_header_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text(
                "---\n"
                "version: v1.0.0\n"
                "status: running\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: mgr:1\n"
                "is_manager: false\n"
                "pending_task_items: []\n"
                "---\n"
                "old goal\n"
                "- old item\n",
                encoding="utf-8",
            )
            prompt = root / "prompt.md"
            prompt.write_text("new followup\n- route it\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", ())
            validate_inputs(args)
            ensure_task_file(args, "cfg:2")
            self.assertTrue(path.read_text(encoding="utf-8").endswith("old goal\n- old item\nnew followup\n- route it\n"))

    def test_existing_frontmatter_task_launch_updates_frontmatter_without_legacy_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text(
                "---\n"
                "version: v1.0.0\n"
                "status: blocked\n"
                "blocked_on: waiting on restart\n"
                "runat: cfg:2\n"
                "tool: codex\n"
                "managerat: mgr:1\n"
                "is_manager: false\n"
                "pending_task_items: []\n"
                "---\n"
                "old goal\n"
                "- old item\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "7", "pcodx", root, "", None, False, False, "", "medium", (), True, "mgr:1", model="gpt-5.6-terra")
            validate_inputs(args)
            ensure_task_file(args, "cfg:7")
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"))
            self.assertNotIn("blocked_on:", text)
            metadata = parse_task_metadata(text)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("running", metadata.status)
            self.assertEqual("cfg:7", metadata.runat)
            self.assertEqual("pcodx", metadata.tool)
            self.assertEqual("mgr:1", metadata.managerat)

    def test_new_task_file_requires_goal_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Args(root, "x.md", "cfg", "2", "pcodx", None, "", None, False, False, "", "", (), False, "mgr:1")
            with self.assertRaisesRegex(ValueError, "high-level goal"):
                ensure_task_file(args, "cfg:2")

    def test_existing_task_file_header_tracks_latest_launch_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text("runat: cfg:2 codex\n\nold body\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "pcodx", root, "", None, False, False, "", "", ())
            self.assertEqual(path, ensure_task_file(args, "cfg:2"))
            self.assertEqual("runat: cfg:2 pcodx\n\nold body\n", path.read_text(encoding="utf-8"))

    def test_metadata_only_existing_task_preserves_runat_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text("runat: cfg:2 codex\n\nold body\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "pcodx", None, "", None, False, False, "", "", ())
            self.assertEqual(path, ensure_task_file(args, "cfg:2"))
            self.assertEqual("runat: cfg:2 codex\n\nold body\n", path.read_text(encoding="utf-8"))

    def test_new_window_uses_tmux_new_window_shape_without_starting_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, False, '', '', ())
            session_path = subprocess.CompletedProcess(["tmux"], 0, f"$1\t{tmp}\n", "")
            created = subprocess.CompletedProcess(["tmux"], 0, "cfg:7\n", "")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="cfg"), patch(
                'omo_manager.omo_task.tmux', side_effect=[session_path, created]
            ) as tmux, patch('omo_manager.omo_task.wait_shell') as wait_shell, patch('omo_manager.omo_task.start_codex') as start_codex_mock:
                self.assertEqual('cfg:7', new_window(args))
            command = tmux.call_args_list[1].args[0]
            self.assertEqual(['new-window', '-P'], command[:2])
            self.assertNotIn('bunx @openai/codex --dangerously-bypass-approvals-and-sandbox', command)
            wait_shell.assert_called_once_with('cfg:7')
            start_codex_mock.assert_not_called()

    def test_new_window_creates_missing_named_session_at_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "newcfg", "", "codex", Path(tmp), "worker", None, False, False, "", "", ())
            missing = subprocess.CompletedProcess(["tmux"], 1, "", "can't find session: newcfg")
            created = subprocess.CompletedProcess(["tmux"], 0, "newcfg:0\n", "")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="newcfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[missing, created]
            ) as tmux_mock, patch("omo_manager.omo_task.wait_shell") as wait_shell:
                self.assertEqual("newcfg:0", new_window(args))
            command = tmux_mock.call_args_list[1].args[0]
            self.assertEqual(["new-session", "-d", "-P"], command[:3])
            self.assertEqual("newcfg", command[command.index("-s") + 1])
            self.assertEqual(str(Path(tmp)), command[command.index("-c") + 1])
            wait_shell.assert_called_once_with("newcfg:0")

    def test_blank_session_display_creates_only_after_exact_absence_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "newcfg", "", "codex", Path(tmp), "worker", None, False, False, "", "", ())
            blank = subprocess.CompletedProcess(["tmux"], 0, "\t\n", "")
            missing = subprocess.CompletedProcess(["tmux"], 1, "", "can't find session: newcfg")
            created = subprocess.CompletedProcess(["tmux"], 0, "newcfg:0\n", "")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="newcfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[blank, missing, created]
            ) as tmux_mock, patch("omo_manager.omo_task.wait_shell"):
                self.assertEqual("newcfg:0", new_window(args))
            self.assertEqual(["has-session", "-t", "=newcfg:"], tmux_mock.call_args_list[1].args[0])
            self.assertEqual("new-session", tmux_mock.call_args_list[2].args[0][0])

    def test_blank_session_display_for_existing_session_remains_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "cfg", "", "codex", Path(tmp), "worker", None, False, False, "", "", ())
            blank = subprocess.CompletedProcess(["tmux"], 0, "\t\n", "")
            exists = subprocess.CompletedProcess(["tmux"], 0, "", "")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="cfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[blank, exists]
            ) as tmux_mock, self.assertRaisesRegex(RuntimeError, "did not report one usable session_id and session_path"):
                _ = new_window(args)
            self.assertEqual(["has-session", "-t", "=cfg:"], tmux_mock.call_args_list[1].args[0])
            self.assertEqual(2, tmux_mock.call_count)

    def test_blank_missing_session_keeps_requested_window_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "newcfg", "4", "codex", Path(tmp), "worker", None, False, False, "", "", ())
            blank = subprocess.CompletedProcess(["tmux"], 0, "\t\n", "")
            missing = subprocess.CompletedProcess(["tmux"], 1, "", "can't find session: newcfg")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="newcfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[blank, missing]
            ) as tmux_mock, self.assertRaisesRegex(ValueError, "cannot create missing tmux session `newcfg` at requested --tmux-window 4"):
                _ = new_window(args)
            self.assertEqual(["has-session", "-t", "=newcfg:"], tmux_mock.call_args_list[1].args[0])
            self.assertEqual(2, tmux_mock.call_count)

    def test_missing_session_creation_retains_tmux_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "newcfg", "", "codex", Path(tmp), "worker", None, False, False, "", "", ())
            missing = subprocess.CompletedProcess(["tmux"], 1, "", "can't find session: newcfg")
            failure = subprocess.CalledProcessError(1, ["tmux", "new-session"], output="tmux-out", stderr="tmux-err")
            state = subprocess.CompletedProcess(["tmux", "list-windows"], 1, "", "session unavailable")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="newcfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[missing, failure, state]
            ), self.assertRaisesRegex(RuntimeError, r"tmux new-session failed; diagnostic: (.+)") as raised:
                _ = new_window(args)
            evidence = Path(raised.exception.args[0].split("diagnostic: ", 1)[1])
            try:
                text = evidence.read_text(encoding="utf-8")
            finally:
                evidence.unlink(missing_ok=True)
            self.assertIn("omo_task tmux new-session failure", text)
            self.assertIn("tmux_command: tmux new-session -d -P", text)
            self.assertIn("stderr: tmux-err", text)

    def test_new_window_rejects_session_workdir_mismatch_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            args = Args(Path(tmp), "x.md", "cfg", "", "codex", Path(tmp), "worker", None, False, False, "", "", ())
            session_path = subprocess.CompletedProcess(["tmux"], 0, f"$1\t{other}\n", "")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="cfg"), patch(
                "omo_manager.omo_task.tmux", return_value=session_path
            ) as tmux_mock, self.assertRaisesRegex(ValueError, "both must identify the same directory"):
                _ = new_window(args)
            self.assertEqual(["display-message", "-p", "-t", "=cfg:", "#{session_id}\t#{session_path}"], tmux_mock.call_args.args[0])

    def test_new_window_retains_tmux_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "x.md"
            task.write_text(VALID_GOAL_TREE, encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text("launch\n", encoding="utf-8")
            email = root / "human-email.md"
            email.write_text("private email source\n", encoding="utf-8")
            args = replace(
                Args(root, "x.md", "cfg", "", "codex", root, "worker", prompt, False, False, "", "", ()),
                codex_flags=("mcp_servers.private=secret",),
                human_email_file=email,
                human_email_lines=(1, 1),
                human_email_text="private human email text",
            )
            failure = subprocess.CalledProcessError(1, ["tmux", "new-window"], output="tmux-out", stderr="tmux-err")
            state = subprocess.CompletedProcess(["tmux", "list-windows"], 0, "1:manager:bunx:/tmp:%1\n", "")
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), patch(
                "omo_manager.omo_task.tmux", side_effect=[failure, state]
            ):
                with self.assertRaisesRegex(RuntimeError, r"diagnostic: (.+)") as raised:
                    _ = new_window(args)
            evidence = Path(raised.exception.args[0].split("diagnostic: ", 1)[1])
            try:
                text = evidence.read_text(encoding="utf-8")
            finally:
                evidence.unlink(missing_ok=True)
            self.assertIn("exit_status: 1", text)
            self.assertIn("stderr: tmux-err", text)
            self.assertIn("task: path=", text)
            self.assertIn("prompt: path=", text)
            self.assertIn("workdir: path=", text)
            self.assertIn("effective_window_name: worker", text)
            self.assertIn("session_windows_stdout: 1:manager:bunx:/tmp:%1", text)
            self.assertNotIn("private human email text", text)
            self.assertNotIn("mcp_servers.private=secret", text)
            self.assertNotIn(str(email), text)
            self.assertNotIn("private email source", text)

    def test_new_window_retains_tmux_timeout_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Args(root, "x.md", "cfg", "", "codex", root, "worker", None, False, False, "", "", ())
            timeout = subprocess.TimeoutExpired(["tmux", "new-window"], 10, output="partial-out", stderr="partial-err")
            state = subprocess.CompletedProcess(["tmux", "list-windows"], 1, "", "session unavailable")
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), patch(
                "omo_manager.omo_task.tmux", side_effect=[timeout, state]
            ):
                with self.assertRaisesRegex(RuntimeError, r"diagnostic: (.+)") as raised:
                    _ = new_window(args)
            evidence = Path(raised.exception.args[0].split("diagnostic: ", 1)[1])
            try:
                text = evidence.read_text(encoding="utf-8")
            finally:
                evidence.unlink(missing_ok=True)
            self.assertIn("exit_status: timeout after 10s", text)
            self.assertIn("stdout: partial-out", text)
            self.assertIn("stderr: partial-err", text)

    def test_new_window_retains_tmux_spawn_error_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Args(root, "x.md", "cfg", "", "codex", root, "worker", None, False, False, "", "", ())
            failure = FileNotFoundError(2, "tmux unavailable")
            state = subprocess.CompletedProcess(["tmux", "list-windows"], 1, "", "tmux unavailable")
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), patch(
                "omo_manager.omo_task.tmux", side_effect=[failure, state]
            ):
                with self.assertRaisesRegex(RuntimeError, r"diagnostic: (.+)") as raised:
                    _ = new_window(args)
            evidence = Path(raised.exception.args[0].split("diagnostic: ", 1)[1])
            try:
                text = evidence.read_text(encoding="utf-8")
            finally:
                evidence.unlink(missing_ok=True)
            self.assertIn("exit_status: FileNotFoundError: [Errno 2] tmux unavailable", text)

    def test_codex_cmd_resumes_quoted_session(self) -> None:
        self.assertTrue(codex_cmd("abc").startswith("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume abc "))
        self.assertTrue(codex_cmd("abc def").startswith("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume 'abc def' "))
        self.assertTrue(codex_cmd("abc", tool="pcodx").startswith(f"{PCODX_WRAPPER} resume abc "))
        self.assertIn(str(DEFAULT_WORKER_INSTRUCTIONS), codex_cmd("abc", tool="pcodx"))

    def test_codex_cmd_can_resume_without_submitting_prompt(self) -> None:
        self.assertEqual(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume abc",
            codex_cmd("abc", include_prompt=False),
        )

    def test_codex_cmd_resume_binds_requested_workdir_for_codex_only(self) -> None:
        workdir = Path("/tmp/current work")
        self.assertEqual(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --cd '/tmp/current work' resume abc",
            codex_cmd("abc", include_prompt=False, workdir=workdir),
        )
        self.assertNotIn("--cd", codex_cmd("abc", tool="pcodx", include_prompt=False, workdir=workdir))

    def test_resume_idle_requires_session_and_rejects_prompt(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--resume-idle"])
        with self.assertRaises(SystemExit):
            parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--session-id", "abc", "--resume-idle"])
        with self.assertRaises(SystemExit):
            parse_args([
                "--task-file",
                "x.md",
                "--tmux-session",
                "hvl",
                "--workdir",
                "/tmp",
                "--session-id",
                "abc",
                "--resume-idle",
                "--prompt-file",
                "/tmp/prompt",
            ])

    def test_resume_idle_launch_does_not_require_model_override(self) -> None:
        args = parse_args([
            "--task-file",
            "x.md",
            "--tmux-session",
            "cfg",
            "--workdir",
            "/tmp",
            "--session-id",
            "abc",
            "--resume-idle",
        ])
        self.assertTrue(args.resume_idle)

    def test_codex_cmd_uses_prompt_argument_from_file(self) -> None:
        expected_paths = f"{DEFAULT_WORKER_INSTRUCTIONS} /tmp/prompt.md"
        self.assertEqual(
            f'bunx @openai/codex --dangerously-bypass-approvals-and-sandbox "$(cat -- {expected_paths})"',
            codex_cmd(prompt_file=Path("/tmp/prompt.md")),
        )

    def test_codex_cmd_prepends_worker_defaults_to_prompt_file(self) -> None:
        self.assertIn(str(DEFAULT_WORKER_INSTRUCTIONS), codex_cmd(prompt_file=Path("/tmp/prompt.md")))

    def test_codex_cmd_adds_vl_worker_defaults_only_for_vl_agents(self) -> None:
        self.assertNotIn(str(VL_WORKER_INSTRUCTIONS), codex_cmd(prompt_file=Path("/tmp/prompt.md")))
        self.assertEqual(
            f'bunx @openai/codex --dangerously-bypass-approvals-and-sandbox "$(cat -- {DEFAULT_WORKER_INSTRUCTIONS} {VL_WORKER_INSTRUCTIONS} /tmp/prompt.md)"',
            codex_cmd(prompt_file=Path("/tmp/prompt.md"), vl_agent=True),
        )

    def test_codex_cmd_adds_defaults_without_custom_prompt(self) -> None:
        command = codex_cmd(vl_agent=True)
        self.assertIn(str(DEFAULT_WORKER_INSTRUCTIONS), command)
        self.assertIn(str(VL_WORKER_INSTRUCTIONS), command)

    def test_vl_agent_scope_uses_task_file_or_tmux_session(self) -> None:
        self.assertTrue(is_vl_agent("vl_worker.md", "cfg:2"))
        self.assertTrue(is_vl_agent("nested/vl_worker.md", "cfg:2"))
        self.assertTrue(is_vl_agent("worker.md", "vl:2.0"))
        self.assertFalse(is_vl_agent("archive/vl_notes/task.md", "cfg:2"))
        self.assertFalse(is_vl_agent("worker.md", "vl.dev:2"))
        self.assertFalse(is_vl_agent("worker.md", "cfg:2"))

    def test_codex_cmd_adds_reasoning_effort_and_extra_flags(self) -> None:
        self.assertTrue(codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review")).startswith(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
        ))
        self.assertTrue(codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review"), tool="codex").startswith(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
        ))

    def test_codex_cmd_orders_and_quotes_explicit_model_and_effort(self) -> None:
        self.assertTrue(codex_cmd(model="model name", reasoning_effort="xhigh", codex_flags=("--profile", "deep-review")).startswith(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --model 'model name' --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
        ))
        self.assertTrue(codex_cmd(model="model name", reasoning_effort="xhigh", codex_flags=("--profile", "deep-review"), tool="pcodx").startswith(
            f"{PCODX_WRAPPER} --model 'model name' --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
        ))

    def test_codex_cmd_resume_carries_explicit_model_and_effort(self) -> None:
        self.assertTrue(codex_cmd("abc def", reasoning_effort="max", model="gpt-5.6-terra").startswith(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --model gpt-5.6-terra --config 'model_reasoning_effort=\"max\"' resume 'abc def'",
        ))
        self.assertTrue(codex_cmd("abc def", reasoning_effort="max", model="gpt-5.6-terra", tool="pcodx").startswith(
            f"{PCODX_WRAPPER} --model gpt-5.6-terra --config 'model_reasoning_effort=\"max\"' resume 'abc def'",
        ))

    def test_pcodx_tool_uses_wrapper_command(self) -> None:
        self.assertTrue(PCODX_WRAPPER.is_absolute())
        self.assertEqual(Path(__file__).resolve().parents[1] / "pcodx", PCODX_WRAPPER)
        self.assertTrue(codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review"), tool="pcodx").startswith(
            f"{PCODX_WRAPPER} --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
        ))

    def test_pcodx_wrapper_allows_new_reasoning_efforts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            bun = bin_dir / "bun"
            bun.write_text("#!/usr/bin/env bash\nprintf 'developer instructions\\n'\n", encoding="utf-8")
            bun.chmod(0o700)
            captured = root / "captured.txt"
            bunx = bin_dir / "bunx"
            bunx.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {captured}\n", encoding="utf-8")
            bunx.chmod(0o700)
            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "PCODX_POC_ROOT": str(root / "pcodx"),
                "PCODX_RUN_DIR": str(root / "run"),
            }

            result = subprocess.run(
                [str(PCODX_WRAPPER), "--config", 'model_reasoning_effort="max"', "--config=model_reasoning_effort='ultra'", "prompt"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            sent = captured.read_text(encoding="utf-8")
            self.assertIn("check_for_update_on_startup=false", sent)
            self.assertIn('model_reasoning_effort="max"', sent)
            self.assertIn("model_reasoning_effort='ultra'", sent)
            self.assertIn("prompt\n", sent)

    def test_parse_args_accepts_repeatable_codex_flags(self) -> None:
        args = parse_args(
            ["--task-file", "x.md", "--tmux-session", "cfg", "--reasoning-effort", "xhigh", "--codex-flag=--profile", "--codex-flag", "deep-review"]
        )
        self.assertEqual("xhigh", args.reasoning_effort)
        self.assertEqual(("--profile", "deep-review"), args.codex_flags)
        self.assertEqual(DEFAULT_TOOL, args.tool)

    def test_parse_args_requires_exact_tmux_session_name(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            parse_args(["--task-file", "x.md"])
        self.assertIn("--tmux-session is required", stderr.getvalue())
        for invalid in ("=cfg", "cfg:2", "cfg.other", "2cfg"):
            with self.subTest(invalid=invalid), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
                parse_args(["--task-file", "x.md", "--tmux-session", invalid])
            self.assertIn("exact session name", stderr.getvalue())

    def test_parse_args_requires_model_and_reasoning_for_launch(self) -> None:
        base = ["--task-file", "x.md", "--tmux-session", "cfg", "--workdir", "/tmp"]
        for supplied in ((), ("--model", "gpt-5.6-terra"), ("--reasoning-effort", "max")):
            with self.subTest(supplied=supplied), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
                parse_args([*base, *supplied])
            self.assertIn("requires nonempty --model MODEL and --reasoning-effort EFFORT", stderr.getvalue())
        args = parse_args([*base, "--model", "gpt-5.6-terra", "--reasoning-effort", "max"])
        self.assertEqual("gpt-5.6-terra", args.model)
        self.assertEqual("max", args.reasoning_effort)

    def test_validate_inputs_requires_model_and_reasoning_for_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for model, effort in (("", ""), ("gpt-5.6-terra", ""), ("", "max"), (" ", "max")):
                args = Args(root, "x.md", "cfg", "", "codex", root, "", None, False, False, "", effort, (), model=model)
                with self.subTest(model=model, effort=effort), self.assertRaisesRegex(ValueError, "requires nonempty --model MODEL and --reasoning-effort EFFORT"):
                    validate_inputs(args)

    def test_model_must_be_a_safe_identifier(self) -> None:
        for model in (" ", "gpt-5.6-terra\n--profile", "gpt-5.6-terra\r--profile", "gpt-5.6-terra\u2028--profile"):
            with self.subTest(model=model), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
                parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--model", model])
            self.assertIn("nonempty model identifier", stderr.getvalue())
        args = Args(Path("/tmp"), "x.md", "cfg", "2", "codex", None, "", None, False, False, "", "", (), model="bad\nmodel")
        with self.assertRaisesRegex(ValueError, "nonempty model identifier"):
            validate_inputs(args)

    def test_validate_inputs_rejects_worker_launch_in_human_session(self) -> None:
        args = parse_args(
            [
                "--task-file",
                "x.md",
                "--tmux-session",
                "hcfg",
                "--workdir",
                "/tmp",
                "--model",
                "gpt-5.6-sol",
                "--reasoning-effort",
                "medium",
            ]
        )

        with patch(
            "omo_manager.omo_task.subprocess.run",
            return_value=subprocess.CompletedProcess(["tmux"], 0, "hcfg\n", ""),
        ), self.assertRaisesRegex(ValueError, "human-owned `h\\*` tmux sessions"):
            validate_inputs(args)

    def test_validate_inputs_checks_exact_human_session_before_launch(self) -> None:
        args = parse_args(
            [
                "--task-file",
                "x.md",
                "--tmux-session",
                "hcfg",
                "--workdir",
                "/tmp",
                "--model",
                "gpt-5.6-sol",
                "--reasoning-effort",
                "medium",
            ]
        )

        with patch(
            "omo_manager.omo_task.subprocess.run",
            return_value=subprocess.CompletedProcess(["tmux"], 0, "hcfg\n", ""),
        ), self.assertRaisesRegex(ValueError, "authoritative direct launch request"):
            validate_inputs(args)

    def test_validate_inputs_allows_named_human_session_from_authoritative_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir()
            (mail / "request.txt").write_text("Please launch my direct agent in hreview now.\n", encoding="utf-8")
            (root / "x.md").write_text(
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: awaiting relaunch\nrunat: old:1\ntool: codex\n"
                "managerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
                "keep this worker available\n- continue the direct human task\n",
                encoding="utf-8",
            )
            (root / "MANAGER.md").write_text("manager instructions\n", encoding="utf-8")
            args = Args(
                root,
                "x.md",
                "hreview",
                "",
                "codex",
                root,
                "",
                None,
                False,
                False,
                "",
                "medium",
                (),
                model="gpt-5.6-sol",
                manager_target="mgr:1",
                human_email_file=Path("manager_mail/request.txt"),
                human_email_lines=(1, 1),
            )

            with patch(
                "omo_manager.omo_task.subprocess.run",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "hreview\n", ""),
            ):
                self.assertEqual("Please launch my direct agent in hreview now.\n", validate_inputs(args))

    def test_validate_inputs_allows_alternative_explicit_agent_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir()
            request = "Give me a smart interactive agent in hvl to review the bugs to be reported.\n"
            (mail / "request.txt").write_text(request, encoding="utf-8")
            (root / "x.md").write_text(
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: awaiting relaunch\nrunat: old:1\ntool: codex\n"
                "managerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
                "keep this worker available\n- continue the direct human task\n",
                encoding="utf-8",
            )
            args = Args(
                root,
                "x.md",
                "hvl",
                "",
                "codex",
                root,
                "",
                None,
                False,
                False,
                "",
                "medium",
                (),
                model="gpt-5.6-sol",
                manager_target="mgr:1",
                human_email_file=Path("manager_mail/request.txt"),
                human_email_lines=(1, 1),
            )

            with patch(
                "omo_manager.omo_task.subprocess.run",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "hvl\n", ""),
            ):
                self.assertEqual(request, validate_inputs(args))

            punctuated = "Give me a smart interactive agent in hvl.\n"
            (mail / "request.txt").write_text(punctuated, encoding="utf-8")
            with patch(
                "omo_manager.omo_task.subprocess.run",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "hvl\n", ""),
            ):
                self.assertEqual(punctuated, validate_inputs(args))

    def test_validate_inputs_allows_just_create_worker_in_named_human_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir()
            request = "Just create a worker at hwl to replace the previous one.\n"
            (mail / "request.txt").write_text(request, encoding="utf-8")
            (root / "x.md").write_text(
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: missing pane\nrunat: hwl:3\ntool: pcodx\n"
                "managerat: mgr:1\nis_manager: true\npending_task_items: []\n---\n"
                "resume the planner\n- preserve its queue\n",
                encoding="utf-8",
            )
            (root / "MANAGER.md").write_text("manager instructions\n", encoding="utf-8")
            args = Args(
                root, "x.md", "hwl", "", "pcodx", root, "", None, False, False, "", "max", (),
                model="gpt-5.6-sol", manager_target="mgr:1", is_manager=True,
                human_email_file=Path("manager_mail/request.txt"), human_email_lines=(1, 1),
            )
            with patch(
                "omo_manager.omo_task.subprocess.run",
                return_value=subprocess.CompletedProcess(["tmux"], 0, "hwl\n", ""),
            ):
                self.assertEqual(request, validate_inputs(args))

    def test_validate_inputs_rejects_non_authorizing_human_session_mentions(self) -> None:
        for excerpt in (
            "hreview is unavailable.\n",
            "Do not launch hreview.\n",
            "> Please launch hreview.\n",
            "Please launch no worker in hreview.\n",
            "Please launch hreview, but do not start anything there.\n",
            "Please launch hreview, but don’t start anything there.\n",
            "Please launch hreview, but won’t that alter the session?\n",
            "Please launch hreview, but shouldn’t we wait?\n",
            "Give me an agent in hother to review hreview.\n",
            "Give me an agent to review hreview, please.\n",
            "I need a worker for hreview, please.\n",
            "Launch hother to review hreview.\n",
        ):
            with self.subTest(excerpt=excerpt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                mail = root / "manager_mail"
                mail.mkdir()
                (mail / "request.txt").write_text(excerpt, encoding="utf-8")
                (root / "x.md").write_text(
                    "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: awaiting relaunch\nrunat: old:1\ntool: codex\n"
                    "managerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
                    "keep this worker available\n- continue the direct human task\n",
                    encoding="utf-8",
                )
                args = Args(
                    root,
                    "x.md",
                    "hreview",
                    "",
                    "codex",
                    root,
                    "",
                    None,
                    False,
                    False,
                    "",
                    "medium",
                    (),
                    model="gpt-5.6-sol",
                    manager_target="mgr:1",
                    human_email_file=Path("manager_mail/request.txt"),
                    human_email_lines=(1, 1),
                )

                with patch(
                    "omo_manager.omo_task.subprocess.run",
                    return_value=subprocess.CompletedProcess(["tmux"], 0, "hreview\n", ""),
                ), self.assertRaisesRegex(ValueError, "authoritative direct launch request"):
                    validate_inputs(args)

    def test_new_window_binds_the_validated_existing_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "cfg", "9", "codex", Path(tmp), "x", None, False, False, "", "medium", (), model="gpt-5.6-sol")
            session_path = subprocess.CompletedProcess(["tmux"], 0, f"$1\t{tmp}\n", "")
            created = subprocess.CompletedProcess(["tmux"], 0, "cfg:9\n", "")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="cfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[session_path, created]
            ) as tmux, patch("omo_manager.omo_task.wait_shell"):
                self.assertEqual("cfg:9", new_window(args))
            self.assertEqual("$1:9", tmux.call_args_list[1].args[0][5])

    def test_replaced_existing_session_fails_without_retargeting_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "cfg", "", "codex", Path(tmp), "x", None, False, False, "", "medium", (), model="gpt-5.6-sol")
            session_path = subprocess.CompletedProcess(["tmux"], 0, f"$1\t{tmp}\n", "")
            failure = subprocess.CalledProcessError(1, ["tmux", "new-window"], stderr="can't find session: $1")
            replacement = subprocess.CompletedProcess(["tmux", "list-windows"], 1, "", "can't find session: $1")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="cfg"), patch(
                "omo_manager.omo_task.tmux", side_effect=[session_path, failure, replacement]
            ) as tmux_mock, self.assertRaisesRegex(RuntimeError, r"diagnostic: (.+)") as raised:
                _ = new_window(args)
            evidence = Path(raised.exception.args[0].split("diagnostic: ", 1)[1])
            evidence.unlink(missing_ok=True)
            command = tmux_mock.call_args_list[1].args[0]
            self.assertEqual("$1", command[command.index("-t") + 1])
            self.assertNotIn("=cfg", command)

    def test_new_window_rechecks_human_session_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "cfg", "", "codex", Path(tmp), "x", None, False, False, "", "medium", (), model="gpt-5.6-sol")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="hcfg"), patch("omo_manager.omo_task.tmux") as tmux, self.assertRaisesRegex(
                ValueError, "human-owned `h\\*` tmux sessions"
            ):
                new_window(args)
            tmux.assert_not_called()

    def test_raw_model_flags_are_rejected(self) -> None:
        raw_argvs = (
            ("--codex-flag=--model", "--codex-flag", "gpt-5.6-terra"),
            ("--codex-flag=--model=gpt-5.6-terra",),
            ("--codex-flag=-m", "--codex-flag", "gpt-5.6-terra"),
            ("--codex-flag=-mgpt-5.6-terra",),
            ("--codex-flag=-m=gpt-5.6-terra",),
        )
        for raw_argv in raw_argvs:
            with self.subTest(raw_argv=raw_argv), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
                parse_args(["--task-file", "x.md", "--tmux-session", "cfg", *raw_argv])
            self.assertIn("use --model MODEL", stderr.getvalue())

    def test_programmatic_raw_model_flags_are_rejected(self) -> None:
        args = Args(Path("/tmp"), "x.md", "cfg", "2", "codex", None, "", None, False, False, "", "", ("--model", "gpt-5.6-terra"))
        with self.assertRaisesRegex(ValueError, "use --model MODEL"):
            validate_inputs(args)

    def test_registration_and_migration_do_not_require_launch_selection(self) -> None:
        registration = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--tmux-window", "2"])
        self.assertEqual("", registration.model)
        self.assertEqual("", registration.reasoning_effort)
        migration = parse_args(
            ["--task-file", "x.md", "--migrate-manager-owner", "--old-manager-target", "cfg:1", "--new-manager-target", "cfg:2"]
        )
        self.assertTrue(migration.migrate_manager_owner)

    def test_migration_runs_without_launch_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "x.md"
            original = (
                "---\n"
                "version: v1.0.0\n"
                "status: running\n"
                "runat: cfg:3\n"
                "tool: codex\n"
                "managerat: cfg:1\n"
                "is_manager: false\n"
                "pending_task_items: []\n"
                "---\n"
                "work\n"
            )
            task.write_text(original, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--migrate-manager-owner",
                        "--old-manager-target",
                        "cfg:1",
                        "--new-manager-target",
                        "cfg:2",
                    ]
                )
            self.assertEqual(0, result)
            self.assertEqual(original.replace("managerat: cfg:1", "managerat: cfg:2"), task.read_text(encoding="utf-8"))

    def test_migration_rejects_model(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "x.md",
                    "--migrate-manager-owner",
                    "--old-manager-target",
                    "cfg:1",
                    "--new-manager-target",
                    "cfg:2",
                    "--model",
                    "gpt-5.6-terra",
                ]
            )
        self.assertIn("only accepts", stderr.getvalue())

    def test_parse_args_accepts_new_reasoning_efforts(self) -> None:
        for effort in ("max", "ultra"):
            with self.subTest(effort=effort):
                args = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--reasoning-effort", effort])
                self.assertEqual(effort, args.reasoning_effort)
                self.assertIn(f'model_reasoning_effort="{effort}"', codex_cmd(reasoning_effort=effort))

    def test_parse_args_accepts_prelaunch_source(self) -> None:
        args = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--prelaunch-source", "/tmp/pre launch.sh"])
        self.assertEqual(Path("/tmp/pre launch.sh"), args.prelaunch_source)

    def test_parse_args_accepts_paired_human_email_options(self) -> None:
        args = parse_args(
            [
                "--root",
                "/tmp/root",
                "--task-file",
                "x.md",
                "--tmux-session",
                "cfg",
                "--workdir",
                "/tmp",
                "--model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "medium",
                "--human-email-file",
                "manager_mail/request.md",
                "--human-email-lines",
                "2-4",
            ]
        )
        self.assertEqual(Path("manager_mail/request.md"), args.human_email_file)
        self.assertEqual((2, 4), args.human_email_lines)

    def test_parse_args_requires_paired_human_email_options_and_valid_range(self) -> None:
        invalid_options = (
            ("--human-email-file", "request.md"),
            ("--human-email-lines", "2-4"),
            ("--human-email-file", "request.md", "--human-email-lines", "0-2"),
            ("--human-email-file", "request.md", "--human-email-lines", "3-2"),
            ("--human-email-file", "request.md", "--human-email-lines", "1:2"),
        )
        for options in invalid_options:
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(["--task-file", "x.md", "--tmux-session", "cfg", *options])

    def test_migration_rejects_human_email_options(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "x.md",
                    "--migrate-manager-owner",
                    "--old-manager-target",
                    "cfg:1",
                    "--new-manager-target",
                    "cfg:2",
                    "--human-email-file",
                    "request.md",
                    "--human-email-lines",
                    "1-1",
                ]
            )
        self.assertIn("only accepts", stderr.getvalue())

    def test_human_email_validation_requires_contained_readable_existing_range(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir()
            email = mail / "request.md"
            email.write_text("one\ntwo\n", encoding="utf-8")
            base = Args(root, "x.md", "cfg", "2", "codex", root, "", None, False, False, "", "medium", (), model="gpt-5.6-terra")
            cases = (
                (replace(base, human_email_file=Path("../request.md"), human_email_lines=(1, 1)), "inside ROOT/manager_mail"),
                (replace(base, human_email_file=Path("manager_mail/missing.md"), human_email_lines=(1, 1)), "not found"),
                (replace(base, human_email_file=Path("manager_mail/request.md"), human_email_lines=(1, 3)), "only 2 lines"),
            )
            for args, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    validate_inputs(args)
            with patch("omo_manager.omo_task.os.access", return_value=False), self.assertRaisesRegex(ValueError, "not readable"):
                validate_inputs(replace(base, human_email_file=Path("manager_mail/request.md"), human_email_lines=(1, 1)))

    def test_human_email_options_require_actual_launch(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "x.md",
                    "--tmux-session",
                    "cfg",
                    "--human-email-file",
                    "manager_mail/request.md",
                    "--human-email-lines",
                    "1-1",
                ]
            )
        self.assertIn("require --workdir", stderr.getvalue())

    def test_invalid_human_email_is_rejected_before_task_todo_or_tmux_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manager_mail").mkdir()
            (root / "manager_mail" / "request.md").write_text("one\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), patch("omo_manager.omo_task.tmux") as tmux_mock:
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "cfg",
                        "--workdir",
                        str(root),
                        "--model",
                        "gpt-5.6-terra",
                        "--reasoning-effort",
                        "medium",
                        "--prompt-file",
                        str(prompt),
                        "--human-email-file",
                        "manager_mail/request.md",
                        "--human-email-lines",
                        "1-2",
                    ]
                )
            self.assertEqual(1, result)
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())
            tmux_mock.assert_not_called()

    def test_prompt_orders_defaults_manager_custom_and_exact_human_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "MANAGER.md"
            custom = root / "custom.md"
            manager.write_bytes(b"MANAGER\n")
            custom.write_bytes(b"CUSTOM\n")
            excerpt = "human first\r\nhuman second\r\n"
            human_instruction = write_human_instruction_file(excerpt)
            try:
                rendered = self.render_prompt(prompt_input(custom, vl_agent=True, manager_file=manager, human_instruction_file=human_instruction))
                expected = (
                    DEFAULT_WORKER_INSTRUCTIONS.read_bytes()
                    + VL_WORKER_INSTRUCTIONS.read_bytes()
                    + manager.read_bytes()
                    + custom.read_bytes()
                    + b'\n<human_instruction authoritative="true">\n'
                    + excerpt.encode()
                    + b"</human_instruction>"
                )
                self.assertEqual(expected, rendered)
                self.assertEqual(0o600, human_instruction.stat().st_mode & 0o777)
            finally:
                human_instruction.unlink(missing_ok=True)

    def test_human_excerpt_preserves_selected_lines_without_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir()
            email = mail / "private-source-name.md"
            email.write_bytes(b"ignore\r\nexact one\r\nexact two")
            (root / "x.md").write_text("runat: cfg:2 codex\nwork\n- item\n", encoding="utf-8")
            args = Args(
                root,
                "x.md",
                "cfg",
                "2",
                "codex",
                root,
                "",
                None,
                False,
                False,
                "",
                "medium",
                (),
                model="gpt-5.6-terra",
                human_email_file=Path("manager_mail/private-source-name.md"),
                human_email_lines=(2, 3),
            )
            excerpt = validate_inputs(args)
            human_instruction = write_human_instruction_file(excerpt)
            try:
                rendered = self.render_prompt(prompt_input(None, human_instruction_file=human_instruction))
                self.assertTrue(rendered.endswith(b'<human_instruction authoritative="true">\nexact one\r\nexact two</human_instruction>'))
                self.assertNotIn(b"private-source-name.md", rendered)
                self.assertNotIn(b"2-3", rendered)
            finally:
                human_instruction.unlink(missing_ok=True)

    def test_human_excerpt_rejects_closing_delimiter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mail = root / "manager_mail"
            mail.mkdir()
            (mail / "request.md").write_text("safe\n</human_instruction>\n", encoding="utf-8")
            args = Args(
                root,
                "x.md",
                "cfg",
                "2",
                "codex",
                root,
                "",
                None,
                False,
                False,
                "",
                "medium",
                (),
                model="gpt-5.6-terra",
                human_email_file=Path("manager_mail/request.md"),
                human_email_lines=(1, 2),
            )
            with self.assertRaisesRegex(ValueError, "must not contain"):
                validate_inputs(args)

    def test_manager_instructions_are_required_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), patch("omo_manager.omo_task.tmux") as tmux_mock:
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "cfg",
                        "--workdir",
                        str(root),
                        "--model",
                        "gpt-5.6-terra",
                        "--reasoning-effort",
                        "medium",
                        "--prompt-file",
                        str(prompt),
                        "--is-manager",
                    ]
                )
            self.assertEqual(1, result)
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())
            tmux_mock.assert_not_called()

    def test_registration_only_manager_does_not_require_manager_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.md").write_text("runat: cfg:2 codex\nwork\n- item\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", None, False, False, "", "", (), is_manager=True)
            self.assertEqual("", validate_inputs(args))

    def test_task_file_bookkeeping_stores_only_custom_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MANAGER.md").write_text("manager-only-secret\n", encoding="utf-8")
            mail = root / "manager_mail"
            mail.mkdir()
            (mail / "request.md").write_text("email-only-secret\n", encoding="utf-8")
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(
                root,
                "x.md",
                "cfg",
                "2",
                "codex",
                None,
                "",
                prompt,
                False,
                False,
                "",
                "",
                (),
                False,
                "mgr:1",
                None,
                True,
                human_email_file=Path("request.md"),
                human_email_lines=(1, 1),
            )
            text = ensure_task_file(args, "cfg:2").read_text(encoding="utf-8")
            self.assertIn(VALID_GOAL_TREE, text)
            self.assertNotIn("manager-only-secret", text)
            self.assertNotIn("email-only-secret", text)
            self.assertNotIn(DEFAULT_WORKER_INSTRUCTIONS.read_text(encoding="utf-8"), text)

    def test_parse_args_resolves_relative_prelaunch_source(self) -> None:
        args = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--prelaunch-source", "omo_manager/WORKER_DEFAULTS.md"])
        self.assertEqual((Path.cwd() / "omo_manager" / "WORKER_DEFAULTS.md").resolve(), args.prelaunch_source)

    def test_parse_args_rejects_removed_vl_preflight_flags(self) -> None:
        for flag in (
            "--vl-experiment-preflight",
            "--vl-preflight-vlh",
            "--vl-preflight-verus",
            "--vl-preflight-artifact-root",
        ):
            argv = ["--task-file", "vl_worker_exp_1.md", flag]
            if flag != "--vl-experiment-preflight":
                argv.append("/tmp/value")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(argv)

    def test_prelaunch_source_must_exist_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--prompt-file",
                            str(prompt),
                            "--prelaunch-source",
                            str(root / "missing.sh"),
                        ]
                    ),
                )
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_parse_args_accepts_pcodx_tool(self) -> None:
        args = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--tool", "pcodx"])
        self.assertEqual("pcodx", args.tool)
        self.assertTrue(args.tool_explicit)

    def test_parse_args_accepts_is_manager(self) -> None:
        args = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--is-manager"])
        self.assertTrue(args.is_manager)

    def test_new_task_file_writes_is_manager_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "mgr:1", None, True)
            path = ensure_task_file(args, "cfg:2")
            metadata = parse_task_metadata(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertTrue(metadata.is_manager)

    def test_new_manager_task_rejects_self_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "cfg:2", None, True)
            with self.assertRaisesRegex(ValueError, "must be different"):
                ensure_task_file(args, "cfg:2")

    def test_new_worker_task_still_rejects_self_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "cfg:2")
            with self.assertRaisesRegex(ValueError, "must be different"):
                ensure_task_file(args, "cfg:2")

    def test_main_success_assigns_pending_queue_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), patch("omo_manager.omo_task.exact_pane_id", return_value="%2"), patch(
                "omo_manager.omo_task.capture_pane", return_value=["ready"]
            ), patch("omo_manager.omo_task.status", return_value="ready"):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--tmux-window",
                            "2",
                            "--prompt-file",
                            str(prompt),
                            "--manager-target",
                            "mgr:1",
                        ]
                    ),
                )
            self.assertIn("launched agent owns its open-work queue through omo_pending.py", out.getvalue())

    def test_main_records_task_before_starting_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            events: list[str] = []

            def fake_ensure(_args: Args, _target: str) -> Path:
                events.append("ensure_task_file")
                return root / "x.md"

            def fake_link(_args: Args, _target: str) -> None:
                events.append("link_todo")

            def fake_start(_target: str, _args: Args) -> None:
                events.append("start_codex")

            out = io.StringIO()
            with (
                patch("omo_manager.omo_task.new_window", return_value="cfg:7") as new_window_mock,
                patch("omo_manager.omo_task.ensure_task_file", side_effect=fake_ensure),
                patch("omo_manager.omo_task.link_todo", side_effect=fake_link),
                patch("omo_manager.omo_task.start_codex", side_effect=fake_start),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--prompt-file",
                            str(prompt),
                            "--manager-target",
                            "mgr:1",
                        ]
                    ),
                )
            new_window_mock.assert_called_once()
            self.assertEqual(["ensure_task_file", "link_todo", "start_codex"], events)
            self.assertIn("wait patiently for the agent to report instead of eagerly checking its status", out.getvalue())

    def test_main_resume_idle_does_not_promise_agent_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = io.StringIO()
            (root / "x.md").write_text(
                "---\nversion: v1.0.0\nstatus: blocked\nblocked_on: idle session\nrunat: cfg:7\ntool: codex\n"
                "managerat: mgr:1\nis_manager: false\npending_task_items: []\n---\n"
                "resume the existing session\n- restore the idle agent\n",
                encoding="utf-8",
            )
            with (
                patch("omo_manager.omo_task.new_window", return_value="cfg:7"),
                patch("omo_manager.omo_task.ensure_task_file", return_value=root / "x.md"),
                patch("omo_manager.omo_task.link_todo"),
                patch("omo_manager.omo_task.start_codex"),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--session-id",
                            "abc",
                            "--resume-idle",
                            "--manager-target",
                            "mgr:1",
                        ]
                    ),
                )
            self.assertNotIn("wait patiently for the agent to report", out.getvalue())

    def test_main_does_not_start_codex_when_task_link_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            with (
                patch("omo_manager.omo_task.new_window", return_value="cfg:7"),
                patch("omo_manager.omo_task.ensure_task_file", return_value=root / "x.md"),
                patch("omo_manager.omo_task.link_todo", side_effect=RuntimeError("todo write failed")),
                patch("omo_manager.omo_task.start_codex") as start_codex_mock,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--prompt-file",
                            str(prompt),
                            "--manager-target",
                            "mgr:1",
                        ]
                    ),
                )
            start_codex_mock.assert_not_called()

    def test_main_relaunch_updates_todo_before_starting_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "x.md"
            task.write_text(
                "---\n"
                "version: v1.0.0\n"
                "status: blocked\n"
                "blocked_on: old blocker\n"
                "runat: cfg:1\n"
                "tool: codex\n"
                "managerat: mgr:1\n"
                "is_manager: false\n"
                "pending_task_items: []\n"
                "---\n"
                "old body\n",
                encoding="utf-8",
            )
            (root / "TODO.md").write_text("current:\nx.md cfg:1\n", encoding="utf-8")
            events: list[str] = []

            def fake_start(_target: str, _args: Args) -> None:
                metadata = parse_task_metadata(task.read_text(encoding="utf-8"))
                self.assertIsNotNone(metadata)
                assert metadata is not None
                events.append(f"{metadata.status} {metadata.runat} | {(root / 'TODO.md').read_text(encoding='utf-8').strip()}")

            with (
                patch("omo_manager.omo_task.new_window", return_value="cfg:7"),
                patch("omo_manager.omo_task.start_codex", side_effect=fake_start),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                        ]
                    ),
                )
            metadata = parse_task_metadata(task.read_text(encoding="utf-8"))
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual("running", metadata.status)
            self.assertEqual("cfg:7", metadata.runat)
            self.assertEqual("running cfg:7 | current:\nx.md cfg:7", events[0])

    def test_dry_run_prints_prelaunch_source_before_worker_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            prelaunch = root / "pre launch.sh"
            prelaunch.write_text("export PROJECT_READY=1\n", encoding="utf-8")
            out = io.StringIO()
            argv = [
                "--root",
                str(root),
                "--task-file",
                "x.md",
                "--tmux-session",
                "cfg",
                "--tmux-window",
                "2",
                "--manager-target",
                "mgr:1",
                "--workdir",
                str(root),
                "--model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "medium",
                "--prompt-file",
                str(prompt),
                "--prelaunch-source",
                str(prelaunch),
                "--dry-run",
            ]
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), contextlib.redirect_stdout(out):
                self.assertEqual(0, main(argv))
            text = out.getvalue()
            self.assertIn(f"prelaunch_source: {prelaunch}", text)
            launch_line = next(line for line in text.splitlines() if "tmux send-keys" in line)
            source_idx = launch_line.index("source ")
            prelaunch_idx = launch_line.index(str(prelaunch))
            export_idx = launch_line.index("export OMO_AGENT_TMUX_TARGET=cfg:2")
            marker_idx = launch_line.index("[omo:DRY]")
            exec_idx = launch_line.index("exec bunx @openai/codex")
            self.assertLess(source_idx, prelaunch_idx)
            self.assertLess(prelaunch_idx, export_idx)
            self.assertLess(export_idx, marker_idx)
            self.assertLess(marker_idx, exec_idx)
            self.assertLess(export_idx, exec_idx)

    def test_session_resume_ignores_existing_pcodx_task_tool_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text("runat: cfg:2 pcodx\n\nold body\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", root, "", None, False, False, "11111111-1111-1111-1111-111111111111", "", ())
            self.assertEqual("codex", effective_tool(args))
            ensure_task_file(args, "cfg:2")
            self.assertEqual("runat: cfg:2 codex\n\nold body\n", path.read_text(encoding="utf-8"))

    def test_session_resume_ignores_legacy_runat_pcodx_tool_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text("old notes\nrunat: cfg:2 pcodx\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", root, "", None, False, False, "11111111-1111-1111-1111-111111111111", "", ())

            self.assertEqual("codex", effective_tool(args))
            ensure_task_file(args, "cfg:2")
            self.assertEqual("runat: cfg:2 codex\n\nold notes\nrunat: cfg:2 pcodx\n", path.read_text(encoding="utf-8"))

    def test_session_resume_explicit_tool_overrides_existing_task_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "x.md"
            path.write_text("runat: cfg:2 codex\n\nold body\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "pcodx", root, "", None, False, False, "11111111-1111-1111-1111-111111111111", "", (), True)
            self.assertEqual("pcodx", effective_tool(args))
            ensure_task_file(args, "cfg:2")
            self.assertEqual("runat: cfg:2 pcodx\n\nold body\n", path.read_text(encoding="utf-8"))

    def test_new_window_can_resume_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, False, '11111111-1111-1111-1111-111111111111', '', ())
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), patch(
                'omo_manager.omo_task.tmux'
            ) as tmux, patch('omo_manager.omo_task.wait_shell'), patch('omo_manager.omo_task.start_codex') as start_codex_mock:
                tmux.return_value.stdout = 'cfg:7\n'
                self.assertEqual('cfg:7', new_window(args))
            start_codex_mock.assert_not_called()

    def test_start_codex_sends_command_inside_existing_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', prompt, False, False, '11111111-1111-1111-1111-111111111111', '', ())
            with patch("omo_manager.omo_task.exact_pane_id", return_value="%7"), patch("omo_manager.omo_task.capture_pane", return_value=[]), patch(
                "omo_manager.omo_task.tmux"
            ) as tmux, patch("omo_manager.omo_task.wait_command_started") as wait_command_started_mock:
                start_codex('cfg:7', args)
            command = tmux.call_args_list[0].args[0]
            self.assertEqual(['send-keys', '-t', '%7'], command[:3])
            self.assertIn('bash -lc', command[3])
            self.assertIn('export OMO_AGENT_TMUX_TARGET=cfg:7', command[3])
            self.assertIn('resume 11111111-1111-1111-1111-111111111111', command[3])
            self.assertIn('$(cat --', command[3])
            self.assertEqual('Enter', command[4])
            wait_command_started_mock.assert_called_once()
            self.assertEqual("cfg:7", wait_command_started_mock.call_args.args[0])
            self.assertEqual("%7", wait_command_started_mock.call_args.kwargs["pane_id"])
            self.assertEqual((), wait_command_started_mock.call_args.kwargs["baseline_lines"])
            launch_marker = wait_command_started_mock.call_args.kwargs["launch_marker"]
            self.assertRegex(launch_marker, r"^\[omo:[0-9a-f]{32}\]$")

    def test_start_codex_resume_idle_submits_no_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "hvl", "9", "codex", Path(tmp), "x", None, False, False, "abc", "", (), resume_idle=True)
            with patch("omo_manager.omo_task.exact_pane_id", return_value="%9"), patch("omo_manager.omo_task.capture_pane", return_value=[]), patch(
                "omo_manager.omo_task.tmux"
            ) as tmux, patch("omo_manager.omo_task.wait_command_started"):
                start_codex("hvl:9", args)
            command = tmux.call_args.args[0][3]
            self.assertIn(f"--cd {tmp}", command)
            self.assertIn("resume abc", command)
            self.assertNotIn("$(cat --", command)

    def test_resume_idle_dry_run_uses_exact_target_and_no_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.md").write_text(VALID_GOAL_TREE, encoding="utf-8")
            out = io.StringIO()
            argv = [
                "--root",
                str(root),
                "--task-file",
                "x.md",
                "--tmux-session",
                "cfg",
                "--tmux-window",
                "9",
                "--workdir",
                str(root),
                "--session-id",
                "abc",
                "--resume-idle",
                "--dry-run",
            ]
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), contextlib.redirect_stdout(out):
                self.assertEqual(0, main(argv))
            text = out.getvalue()
            self.assertIn("todo_line: x.md cfg:9", text)
            self.assertIn("new-window -P -F", text)
            self.assertIn("-t cfg:9", text)
            self.assertIn("send-keys -t cfg:9", text)
            self.assertIn(f"--cd {root}", text)
            self.assertIn("resume abc", text)
            self.assertNotIn("$(cat --", text)

    def test_vl_resume_idle_does_not_require_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "vl_worker.md").write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "9", "codex", root, "x", None, False, False, "abc", "", (), manager_target="mgr:1", resume_idle=True)
            validate_inputs(args)

    def test_new_window_honors_requested_window_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "cfg", "9", "codex", Path(tmp), "x", None, False, False, "abc", "", (), resume_idle=True)
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), patch(
                "omo_manager.omo_task.tmux"
            ) as tmux, patch("omo_manager.omo_task.wait_shell"):
                tmux.return_value.stdout = "cfg:9\n"
                self.assertEqual("cfg:9", new_window(args))
            self.assertEqual("$1:9", tmux.call_args.args[0][5])

    def test_start_codex_relaunches_after_runtime_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), "x.md", "cfg", "", "codex", Path(tmp), "x", prompt, False, False, "", "", ())
            with (
                patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
                patch("omo_manager.omo_task.capture_pane", return_value=[]),
                patch("omo_manager.omo_task.tmux") as tmux,
                patch("omo_manager.omo_task.wait_command_started", side_effect=[CODEX_LAUNCH_UPDATED, CODEX_LAUNCH_STARTED]),
                patch("omo_manager.omo_task.wait_shell") as wait_shell,
            ):
                start_codex("cfg:7", args)
        sent = [call.args[0] for call in tmux.call_args_list]
        self.assertEqual(2, len(sent))
        self.assertNotEqual(sent[0], sent[1])
        self.assertIn("bunx @openai/codex", sent[0][3])
        self.assertIn("bunx @openai/codex", sent[1][3])
        wait_shell.assert_called_once_with("%7", timeout_s=15.0)

    def test_start_codex_uses_private_human_file_across_retry_then_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            excerpt = "human text " * 10_000
            args = Args(root, "x.md", "cfg", "", "codex", root, "x", None, False, False, "", "", (), human_email_text=excerpt)
            created: list[Path] = []
            modes: list[int] = []
            present_during_send: list[bool] = []
            commands: list[str] = []

            def record_instruction_file(text: str) -> Path:
                path = write_human_instruction_file(text)
                created.append(path)
                modes.append(path.stat().st_mode & 0o777)
                return path

            def record_tmux(command: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
                _ = check
                if command[0] == "send-keys":
                    present_during_send.append(created[0].exists())
                    commands.append(command[3])
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("omo_manager.omo_task.write_human_instruction_file", side_effect=record_instruction_file),
                patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
                patch("omo_manager.omo_task.capture_pane", return_value=[]),
                patch("omo_manager.omo_task.tmux", side_effect=record_tmux),
                patch("omo_manager.omo_task.wait_command_started", side_effect=[CODEX_LAUNCH_UPDATED, CODEX_LAUNCH_STARTED]),
                patch("omo_manager.omo_task.wait_shell"),
            ):
                start_codex("cfg:7", args)

            self.assertEqual([0o600], modes)
            self.assertEqual([True, True], present_during_send)
            self.assertEqual(2, len(commands))
            self.assertTrue(all(str(created[0]) in command for command in commands))
            self.assertTrue(all(excerpt not in command for command in commands))
            self.assertFalse(created[0].exists())

    def test_start_codex_stops_when_update_does_not_return_to_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), "x.md", "cfg", "", "codex", Path(tmp), "x", prompt, False, False, "", "", ())
            with (
                patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
                patch("omo_manager.omo_task.capture_pane", return_value=[]),
                patch("omo_manager.omo_task.tmux") as tmux,
                patch("omo_manager.omo_task.wait_command_started", return_value=CODEX_LAUNCH_UPDATED),
                patch("omo_manager.omo_task.wait_shell", side_effect=RuntimeError("no shell")),
            ):
                with self.assertRaisesRegex(RuntimeError, "no shell"):
                    start_codex("cfg:7", args)
        self.assertEqual(1, tmux.call_count)

    def test_start_codex_sources_prelaunch_before_worker_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prelaunch = root / "pre launch.sh"
            args = Args(root, "x.md", "cfg", "", "codex", root, "x", prompt, False, False, "", "", (), False, "", prelaunch)
            with patch("omo_manager.omo_task.exact_pane_id", return_value="%7"), patch("omo_manager.omo_task.capture_pane", return_value=[]), patch(
                "omo_manager.omo_task.tmux"
            ) as tmux, patch("omo_manager.omo_task.wait_command_started"):
                start_codex("cfg:7", args)
            command = tmux.call_args_list[0].args[0][3]
            source_idx = command.index("source ")
            prelaunch_idx = command.index(str(prelaunch))
            export_idx = command.index("export OMO_AGENT_TMUX_TARGET=cfg:7")
            marker_idx = command.index("printf ")
            exec_idx = command.index("exec bunx @openai/codex")
            self.assertLess(source_idx, prelaunch_idx)
            self.assertLess(prelaunch_idx, export_idx)
            self.assertLess(export_idx, marker_idx)
            self.assertLess(marker_idx, exec_idx)
            self.assertLess(export_idx, exec_idx)

    def test_start_codex_adds_vl_guidance_for_vl_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), "x.md", "vl", "", "pcodx", Path(tmp), "x", prompt, False, False, "", "", ())
            with patch("omo_manager.omo_task.exact_pane_id", return_value="%7"), patch("omo_manager.omo_task.capture_pane", return_value=[]), patch(
                "omo_manager.omo_task.tmux"
            ) as tmux, patch("omo_manager.omo_task.wait_command_started"):
                start_codex("vl:7", args)
            command = tmux.call_args_list[0].args[0]
            self.assertIn(str(VL_WORKER_INSTRUCTIONS), command[3])

    def test_start_codex_automatically_adds_root_manager_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = root / "MANAGER.md"
            manager.write_text("manager instructions\n", encoding="utf-8")
            args = Args(root, "x.md", "cfg", "", "codex", root, "x", None, False, False, "", "", (), is_manager=True)
            with patch("omo_manager.omo_task.exact_pane_id", return_value="%7"), patch("omo_manager.omo_task.capture_pane", return_value=[]), patch(
                "omo_manager.omo_task.tmux"
            ) as tmux, patch("omo_manager.omo_task.wait_command_started"):
                start_codex("cfg:7", args)
            command = tmux.call_args_list[0].args[0][3]
            self.assertLess(command.index(str(DEFAULT_WORKER_INSTRUCTIONS)), command.index(str(manager)))

    def test_start_codex_rejects_context_free_vl_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "vl", "", "pcodx", Path(tmp), "x", None, False, False, "", "", ())
            with self.assertRaisesRegex(ValueError, "VL launches require --prompt-file"):
                start_codex("vl:7", args)

    def test_wait_command_started_accepts_visible_codex_status(self) -> None:
        with patch('omo_manager.omo_task.tail', return_value=['› Use /skills to list available skills', '  gpt-5.5']), patch('omo_manager.omo_task.current_command', return_value='bash'), patch('omo_manager.omo_task.time.sleep') as sleep:
            wait_command_started('cfg:7')
            sleep.assert_not_called()

    def test_has_live_codex_launch_requires_exact_package_argv(self) -> None:
        pane = ProcessInfo(100, 1, "S", ("zsh",))
        for argv, expected in (
            (("/usr/bin/bunx", "@openai/codex", "--model", "gpt-5.6-sol"), True),
            (("/usr/bin/bunx", "unrelated-package"), False),
        ):
            with self.subTest(argv=argv):
                processes = {100: pane, 101: ProcessInfo(101, 100, "S", argv)}
                result = subprocess.CompletedProcess([], 0, "100\n", "")
                with patch("omo_manager.omo_task.tmux", return_value=result), patch("omo_manager.omo_task.read_processes", return_value=processes):
                    self.assertEqual(expected, has_live_codex_launch("%7"))

    def test_wait_command_started_updates_codex_runtime(self) -> None:
        launch_marker = "[omo-task-launch:test]"
        update_prompt = [
            launch_marker,
            "Update available! 0.144.1 -> 0.144.3",
            "1. Update now",
            "Press Enter to continue...",
        ]
        update_success = [
            launch_marker,
            "Update ran successfully! Please restart Codex.",
        ]
        with (
            patch("omo_manager.omo_task.tail", side_effect=[update_prompt, update_success]),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.sleep") as sleep,
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker)
        self.assertEqual(CODEX_LAUNCH_UPDATED, result)
        tmux.assert_called_once_with(["send-keys", "-t", "cfg:7", "Enter"], check=True)
        sleep.assert_not_called()

    def test_wait_command_started_joins_narrow_pane_and_uses_exact_split_pane(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        trust_prompt = trust_screen(launch_marker)
        ready = [*trust_prompt, "────", "› Use /skills to list available skills", "  gpt-5.6-sol"]
        screens = iter((trust_prompt, ready))
        capture_commands: list[list[str]] = []

        def capture(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            capture_commands.append(command)
            lines = next(screens) if "-J" in command else [launch_marker[:20], launch_marker[20:], *trust_prompt[1:]]
            return subprocess.CompletedProcess(command, 0, "\n".join(lines), "")

        with (
            patch("omo_manager.omo_task.subprocess.run", side_effect=capture),
            patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.has_live_codex_launch", return_value=True),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.sleep"),
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        self.assertTrue(all(command[:5] == ["tmux", "capture-pane", "-p", "-J", "-t"] for command in capture_commands))
        tmux.assert_called_once_with(["send-keys", "-t", "%7", "Enter"], check=True)

    def test_wait_command_started_accepts_captured_trust_popup_after_marker_disappears(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        ready = ["────", "› Use /skills to list available skills", "  gpt-5.6-sol"]
        with (
            patch("omo_manager.omo_task.capture_pane", side_effect=[CAPTURED_TRUST_POPUP, ready]),
            patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.has_live_codex_launch", return_value=True),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.sleep"),
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        tmux.assert_called_once_with(["send-keys", "-t", "%7", "Enter"], check=True)

    def test_wait_command_started_rejects_markerless_trust_text_from_shell(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=CAPTURED_TRUST_POPUP),
            patch("omo_manager.omo_task.current_command", return_value="bash"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Codex launch not verified"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_not_called()

    def test_wait_command_started_rejects_markerless_trust_popup_already_in_baseline(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=CAPTURED_TRUST_POPUP),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unattributed directory trust confirmation"):
                wait_command_started(
                    "cfg:7",
                    launch_marker=launch_marker,
                    pane_id="%7",
                    baseline_lines=tuple(CAPTURED_TRUST_POPUP),
                )
        tmux.assert_not_called()

    def test_wait_command_started_rejects_markerless_trust_text_from_python(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=CAPTURED_TRUST_POPUP),
            patch("omo_manager.omo_task.current_command", return_value="python"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unattributed directory trust confirmation"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_not_called()

    def test_wait_command_started_rejects_markerless_trust_text_from_other_bunx_process(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=CAPTURED_TRUST_POPUP),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.has_live_codex_launch", return_value=False),
            patch("omo_manager.omo_task.tmux") as tmux,
        ):
            with self.assertRaisesRegex(RuntimeError, "does not contain the launched Codex process"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_not_called()

    def test_wait_command_started_rejects_trust_popup_in_human_owned_session(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=CAPTURED_TRUST_POPUP),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unattributed directory trust confirmation"):
                wait_command_started("hcfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_not_called()

    def test_wait_command_started_rejects_changed_pane_before_trust_enter(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=trust_screen(launch_marker)),
            patch("omo_manager.omo_task.exact_pane_id", return_value="%8"),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.has_live_codex_launch", return_value=True),
            patch("omo_manager.omo_task.tmux") as tmux,
        ):
            with self.assertRaisesRegex(RuntimeError, "no longer identifies launched pane"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_not_called()

    def test_wait_command_started_accepts_wrapped_git_root_note(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        trust_prompt = trust_screen(launch_marker)
        trust_prompt[3:3] = [
            "  Note: You’re in a subdirectory of a Git project. Trusting will apply",
            "  to the repository root: /workspace/project",
            "",
        ]
        ready = [*trust_prompt, "────", "› Use /skills to list available skills", "  gpt-5.6-sol"]
        with (
            patch("omo_manager.omo_task.capture_pane", side_effect=[trust_prompt, ready]),
            patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.has_live_codex_launch", return_value=True),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.sleep"),
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        tmux.assert_called_once_with(["send-keys", "-t", "%7", "Enter"], check=True)

    def test_wait_command_started_rejects_marker_visible_trust_popup_from_shell(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=trust_screen(launch_marker)),
            patch("omo_manager.omo_task.current_command", return_value="bash"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Codex launch not verified"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_not_called()

    def test_wait_command_started_does_not_confirm_unframed_ordinary_output(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        unframed = [
            launch_marker,
            "Do you trust the contents of this directory? Working with untrusted contents comes with higher risk of prompt injection. Trusting the directory allows project-local config, hooks, and exec policies to load.",
            "› 1. Yes, continue",
            "  2. No, quit",
            "Press enter to continue",
        ]
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=unframed),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        tmux.assert_not_called()

    def test_wait_command_started_does_not_confirm_ordinary_output_prepended_to_frame(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        framed = trust_screen(launch_marker)
        framed.insert(1, "ordinary output")
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=framed),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        tmux.assert_not_called()

    def test_wait_command_started_rejects_marker_retained_in_baseline(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=trust_screen(launch_marker)),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Codex launch not verified"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=(launch_marker,))
        tmux.assert_not_called()

    def test_wait_command_started_does_not_repeat_trust_confirmation(self) -> None:
        launch_marker = "[omo:0123456789abcdef0123456789abcdef]"
        trust_prompt = trust_screen(launch_marker)
        with (
            patch("omo_manager.omo_task.capture_pane", return_value=trust_prompt),
            patch("omo_manager.omo_task.exact_pane_id", return_value="%7"),
            patch("omo_manager.omo_task.current_command", return_value="bunx"),
            patch("omo_manager.omo_task.has_live_codex_launch", return_value=True),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 1, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "trust confirmation did not advance"):
                wait_command_started("cfg:7", launch_marker=launch_marker, pane_id="%7", baseline_lines=("shell",))
        tmux.assert_called_once_with(["send-keys", "-t", "%7", "Enter"], check=True)

    def test_wait_command_started_ignores_stale_update_prompt_before_launch_marker(self) -> None:
        launch_marker = "[omo-task-launch:test]"
        lines = [
            "Update available! 0.144.1 -> 0.144.3",
            "1. Update now",
            "Press enter to continue",
            "Update ran successfully! Please restart Codex.",
            launch_marker,
            "› Use /skills to list available skills",
            "  gpt-5.6-luna low",
        ]
        with (
            patch("omo_manager.omo_task.tail", return_value=lines),
            patch("omo_manager.omo_task.current_command", return_value="bash"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.sleep") as sleep,
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker)
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        tmux.assert_not_called()
        sleep.assert_not_called()

    def test_wait_command_started_accepts_marker_after_long_prelaunch_output(self) -> None:
        launch_marker = "[omo:test]"
        lines = [f"prelaunch line {idx}" for idx in range(300)]
        lines.extend([launch_marker, "› Use /skills to list available skills", "  gpt-5.6-luna low"])
        with (
            patch("omo_manager.omo_task.tail", return_value=lines[-200:]),
            patch("omo_manager.omo_task.current_command", return_value="bash"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.sleep") as sleep,
        ):
            result = wait_command_started("cfg:7", launch_marker=launch_marker)
        self.assertEqual(CODEX_LAUNCH_STARTED, result)
        tmux.assert_not_called()
        sleep.assert_not_called()

    def test_wait_command_started_does_not_use_stale_update_prompt_without_launch_marker(self) -> None:
        lines = [
            "Update available! 0.144.1 -> 0.144.3",
            "1. Update now",
            "Press Enter to continue...",
            "Update ran successfully! Please restart Codex.",
        ]
        with (
            patch("omo_manager.omo_task.tail", return_value=lines),
            patch("omo_manager.omo_task.current_command", return_value="bash"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Codex launch not verified"):
                wait_command_started("cfg:7", launch_marker="[omo-task-launch:new]")
        tmux.assert_not_called()

    def test_wait_command_started_does_not_reuse_prior_attempt_marker(self) -> None:
        lines = [
            "[omo:old123]",
            "Update available! 0.144.1 -> 0.144.3",
            "1. Update now",
            "Press Enter to continue...",
            "Update ran successfully! Please restart Codex.",
        ]
        with (
            patch("omo_manager.omo_task.tail", return_value=lines),
            patch("omo_manager.omo_task.current_command", return_value="bash"),
            patch("omo_manager.omo_task.tmux") as tmux,
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 6]),
            patch("omo_manager.omo_task.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Codex launch not verified"):
                wait_command_started("cfg:7", launch_marker="[omo:new456]")
        tmux.assert_not_called()

    def test_wait_command_started_fails_when_shell_remains_active(self) -> None:
        with patch('omo_manager.omo_task.tail', return_value=[]), patch('omo_manager.omo_task.current_command', return_value='bash'), patch('omo_manager.omo_task.time.monotonic', side_effect=[0, 6]), patch('omo_manager.omo_task.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Codex launch not verified'):
                wait_command_started('cfg:7')

    def test_wait_shell_fails_when_shell_never_returns(self) -> None:
        with (
            patch("omo_manager.omo_task.current_command", return_value="codex"),
            patch("omo_manager.omo_task.time.monotonic", side_effect=[0, 1, 6]),
            patch("omo_manager.omo_task.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "did not return to shell"):
                wait_shell("cfg:7")
        sleep.assert_called_once_with(0.25)

    def test_main_dry_run_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            out = io.StringIO()
            with patch("omo_manager.omo_task.launch_session", return_value=LaunchSession("cfg", "$1", False)), contextlib.redirect_stdout(out):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--manager-target",
                            "mgr:1",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--prompt-file",
                            str(prompt),
                            "--dry-run",
                        ]
                    ),
                )
            self.assertIn("tmux new-window", out.getvalue())
            self.assertIn("tmux send-keys", out.getvalue())
            self.assertIn("export OMO_AGENT_TMUX_TARGET=cfg:DRYRUN", out.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_missing_session_dry_run_prints_new_session_plan_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            out = io.StringIO()
            missing = subprocess.CompletedProcess(["tmux"], 1, "", "can't find session: newcfg")
            with patch("omo_manager.omo_task.resolved_launch_session_name", return_value="newcfg"), patch(
                "omo_manager.omo_task.tmux", return_value=missing
            ) as tmux_mock, contextlib.redirect_stdout(out):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "newcfg",
                        "--manager-target",
                        "mgr:1",
                        "--workdir",
                        str(root),
                        "--model",
                        "gpt-5.6-terra",
                        "--reasoning-effort",
                        "medium",
                        "--prompt-file",
                        str(prompt),
                        "--dry-run",
                    ]
                )
            self.assertEqual(0, result)
            self.assertIn("tmux: tmux new-session -d -P", out.getvalue())
            self.assertIn("todo_line: x.md newcfg:DRYRUN", out.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())
            self.assertEqual(1, tmux_mock.call_count)

    def test_main_dry_run_rejects_missing_goal_tree_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("implement manager check\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--manager-target",
                            "mgr:1",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--prompt-file",
                            str(prompt),
                            "--dry-run",
                        ]
                    ),
                )
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_new_task_without_runat_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--manager-target", "mgr:1", "--dry-run"]),
                )
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_validate_inputs_rejects_session_only_runat_for_new_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "", "codex", None, "", prompt, False, False, "", "", (), False, "mgr:1")
            with self.assertRaisesRegex(ValueError, "full tmux target"):
                validate_inputs(args)

    def test_existing_target_mode_rejects_missing_pane_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task.exact_pane_id", return_value=""), contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "wl",
                        "--tmux-window",
                        "2",
                        "--prompt-file",
                        str(prompt),
                        "--manager-target",
                        "wl:1",
                    ]
                )

            self.assertEqual(1, result)
            self.assertIn("does not create or launch `wl:2`", stderr.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_missing_session_without_workdir_cannot_launch_or_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            stderr = io.StringIO()
            with patch("omo_manager.omo_task.exact_pane_id", return_value=""), patch("omo_manager.omo_task.tmux") as tmux_mock, contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "missingcfg",
                        "--tmux-window",
                        "2",
                        "--prompt-file",
                        str(prompt),
                        "--manager-target",
                        "wl:1",
                    ]
                )
            self.assertEqual(1, result)
            self.assertIn("use --workdir to launch a new worker", stderr.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())
            tmux_mock.assert_not_called()

    def test_existing_target_mode_rejects_invalid_states_without_mutation(self) -> None:
        for target_status in ("not_codex", "error", "stuck_input"):
            with self.subTest(target_status=target_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prompt = root / "prompt.md"
                prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
                stderr = io.StringIO()

                with patch("omo_manager.omo_task.exact_pane_id", return_value="%2"), patch(
                    "omo_manager.omo_task.capture_pane", return_value=["pane output"]
                ), patch("omo_manager.omo_task.status", return_value=target_status), contextlib.redirect_stderr(stderr):
                    result = main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "wl",
                            "--tmux-window",
                            "2",
                            "--prompt-file",
                            str(prompt),
                            "--manager-target",
                            "wl:1",
                        ]
                    )

                self.assertEqual(1, result)
                self.assertIn(f"got {target_status}", stderr.getvalue())
                self.assertFalse((root / "x.md").exists())
                self.assertFalse((root / "TODO.md").exists())

    def test_existing_target_mode_accepts_ready_codex_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "wl", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")

            with patch("omo_manager.omo_task.exact_pane_id", return_value="%2"), patch("omo_manager.omo_task.capture_pane", return_value=["ready"]), patch(
                "omo_manager.omo_task.status", return_value="ready"
            ):
                self.assertEqual("%2", validate_existing_target_runtime(args))

    def test_existing_target_mode_accepts_running_codex_pane_and_registers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")

            with patch("omo_manager.omo_task.exact_pane_id", return_value="%2"), patch(
                "omo_manager.omo_task.capture_pane", return_value=["running"]
            ), patch("omo_manager.omo_task.status", return_value="running"), contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "wl",
                        "--tmux-window",
                        "2",
                        "--prompt-file",
                        str(prompt),
                        "--manager-target",
                        "wl:1",
                    ]
                )

            self.assertEqual(0, result)
            self.assertTrue((root / "x.md").is_file())
            self.assertTrue((root / "TODO.md").is_file())

    def test_existing_target_dry_run_does_not_require_live_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")

            with patch("omo_manager.omo_task.exact_pane_id") as pane_lookup, contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "wl",
                        "--tmux-window",
                        "2",
                        "--prompt-file",
                        str(prompt),
                        "--manager-target",
                        "wl:1",
                        "--dry-run",
                    ]
                )

            self.assertEqual(0, result)
            pane_lookup.assert_not_called()
            self.assertFalse((root / "x.md").exists())

    def test_existing_target_mode_rejects_replaced_pane_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            stderr = io.StringIO()

            with patch("omo_manager.omo_task.exact_pane_id", side_effect=["%2", "%3"]), patch(
                "omo_manager.omo_task.capture_pane", return_value=["ready"]
            ), contextlib.redirect_stderr(stderr):
                result = main(
                    [
                        "--root",
                        str(root),
                        "--task-file",
                        "x.md",
                        "--tmux-session",
                        "wl",
                        "--tmux-window",
                        "2",
                        "--prompt-file",
                        str(prompt),
                        "--manager-target",
                        "wl:1",
                    ]
                )

            self.assertEqual(1, result)
            self.assertIn("changed while it was being inspected", stderr.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_body_runat_does_not_supply_frontmatter_runat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("runat: cfg:2 pcodx\nimplement manager check\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--manager-target",
                            "mgr:1",
                            "--prompt-file",
                            str(prompt),
                            "--dry-run",
                        ]
                    ),
                )
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_vl_launch_without_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "vl_worker.md",
                            "--tmux-session",
                            "vl",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--dry-run",
                        ]
                    ),
                )
            self.assertFalse((root / "vl_worker.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_validates_prompt_file_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--prompt-file",
                            str(root / "missing.md"),
                            "--dry-run",
                        ]
                    ),
                )
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_multiline_codex_flag_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    1,
                    main(
                        [
                            "--root",
                            str(root),
                            "--task-file",
                            "x.md",
                            "--tmux-session",
                            "cfg",
                            "--workdir",
                            str(root),
                            "--model",
                            "gpt-5.6-terra",
                            "--reasoning-effort",
                            "medium",
                            "--codex-flag",
                            "bad\nflag",
                            "--dry-run",
                        ]
                    ),
                )
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_rejects_raw_mcp_server_config_without_pcodx_tool(self) -> None:
        args = parse_args(["--task-file", "x.md", "--tmux-session", "cfg", "--codex-flag=--config=mcp_servers.pcodx_partial_compact.command=\"bun\""])
        with self.assertRaisesRegex(ValueError, "MCP server config requires --tool pcodx"):
            validate_inputs(args)

    def test_allows_raw_mcp_server_config_for_explicit_pcodx_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = parse_args(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--tmux-window", "2", "--manager-target", "mgr:1", "--prompt-file", str(prompt), "--tool", "pcodx", "--codex-flag=--config=mcp_servers.pcodx_partial_compact.command=\"bun\""])
            validate_inputs(args)

    def test_rejects_non_codex_tool(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--task-file", "x.md", "--tool", "other"])


if __name__ == '__main__':
    _ = unittest.main()
