import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_agent_status import parse_task_metadata
from omo_manager.omo_task import Args, DEFAULT_TOOL, DEFAULT_WORKER_INSTRUCTIONS, PCODX_WRAPPER, PENDING_TASK_ITEMS_MARKER, VL_WORKER_INSTRUCTIONS, codex_cmd, effective_tool, ensure_task_file, is_vl_agent, link_todo, main, new_window, parse_args, runat_goal_tree_error, runat_header_error, start_codex, validate_inputs, validate_runat_goal_tree, wait_command_started


VALID_GOAL_TREE = "implement manager check\n- reject missing task goal tree\n"


class OmoTaskTests(unittest.TestCase):
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
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "", (), False, "vl:15")
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
            args = Args(root, "x.md", "cfg", "2", "pcodx", root, "", None, False, False, "", "", ())
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

    def test_vl_worker_launch_requires_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "", ())
            with self.assertRaisesRegex(ValueError, "require --manager-target"):
                validate_inputs(args)

    def test_vl_submanager_launch_does_not_require_manager_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "vl_submanager_current_8653.md", "vl", "15", "codex", root, "", prompt, False, False, "", "", ())
            with patch.dict("os.environ", {"OMO_AGENT_TMUX_TARGET": "main:1"}):
                validate_inputs(args)

    def test_existing_vl_worker_launch_writes_missing_managerat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            path = root / "vl_worker.md"
            path.write_text("runat: vl:1 codex\nwork\n- route\n(above are pending task items)\n", encoding="utf-8")
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "", (), False, "vl:15")
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
            args = Args(root, "vl_worker.md", "vl", "1", "codex", root, "", prompt, False, False, "", "", (), False, "vl:15")
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
            args = Args(root, "x.md", "cfg", "7", "pcodx", root, "", None, False, False, "", "", (), True, "mgr:1")
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

    def test_new_window_uses_tmux_new_window_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', None, False, False, '', '', ())
            with patch('omo_manager.omo_task.tmux') as tmux, patch('omo_manager.omo_task.wait_shell') as wait_shell, patch('omo_manager.omo_task.start_codex') as start_codex_mock:
                tmux.return_value.stdout = 'cfg:7\n'
                self.assertEqual('cfg:7', new_window(args))
            command = tmux.call_args.args[0]
            self.assertEqual(['new-window', '-P'], command[:2])
            self.assertNotIn('bunx @openai/codex --dangerously-bypass-approvals-and-sandbox', command)
            wait_shell.assert_called_once_with('cfg:7')
            start_codex_mock.assert_called_once_with('cfg:7', args)

    def test_codex_cmd_resumes_quoted_session(self) -> None:
        self.assertEqual("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume abc", codex_cmd("abc"))
        self.assertEqual("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox resume 'abc def'", codex_cmd("abc def"))
        self.assertEqual(f"{PCODX_WRAPPER} resume abc", codex_cmd("abc", tool="pcodx"))

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

    def test_codex_cmd_does_not_add_context_free_vl_guidance(self) -> None:
        self.assertEqual("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox", codex_cmd(vl_agent=True))

    def test_vl_agent_scope_uses_task_file_or_tmux_session(self) -> None:
        self.assertTrue(is_vl_agent("vl_worker.md", "cfg:2"))
        self.assertTrue(is_vl_agent("nested/vl_worker.md", "cfg:2"))
        self.assertTrue(is_vl_agent("worker.md", "vl:2.0"))
        self.assertFalse(is_vl_agent("archive/vl_notes/task.md", "cfg:2"))
        self.assertFalse(is_vl_agent("worker.md", "vl.dev:2"))
        self.assertFalse(is_vl_agent("worker.md", "cfg:2"))

    def test_codex_cmd_adds_reasoning_effort_and_extra_flags(self) -> None:
        self.assertEqual(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
            codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review")),
        )
        self.assertEqual(
            "bunx @openai/codex --dangerously-bypass-approvals-and-sandbox --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
            codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review"), tool="codex"),
        )

    def test_pcodx_tool_uses_wrapper_command(self) -> None:
        self.assertTrue(PCODX_WRAPPER.is_absolute())
        self.assertEqual(Path(__file__).resolve().parents[1] / "pcodx", PCODX_WRAPPER)
        self.assertEqual(
            f"{PCODX_WRAPPER} --config 'model_reasoning_effort=\"xhigh\"' --profile deep-review",
            codex_cmd(reasoning_effort="xhigh", codex_flags=("--profile", "deep-review"), tool="pcodx"),
        )

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
            self.assertIn('model_reasoning_effort="max"', sent)
            self.assertIn("model_reasoning_effort='ultra'", sent)
            self.assertIn("prompt\n", sent)

    def test_parse_args_accepts_repeatable_codex_flags(self) -> None:
        args = parse_args(["--task-file", "x.md", "--reasoning-effort", "xhigh", "--codex-flag=--profile", "--codex-flag", "deep-review"])
        self.assertEqual("xhigh", args.reasoning_effort)
        self.assertEqual(("--profile", "deep-review"), args.codex_flags)
        self.assertEqual(DEFAULT_TOOL, args.tool)

    def test_parse_args_accepts_new_reasoning_efforts(self) -> None:
        for effort in ("max", "ultra"):
            with self.subTest(effort=effort):
                args = parse_args(["--task-file", "x.md", "--reasoning-effort", effort])
                self.assertEqual(effort, args.reasoning_effort)
                self.assertIn(f'model_reasoning_effort="{effort}"', codex_cmd(reasoning_effort=effort))

    def test_parse_args_accepts_prelaunch_source(self) -> None:
        args = parse_args(["--task-file", "x.md", "--prelaunch-source", "/tmp/pre launch.sh"])
        self.assertEqual(Path("/tmp/pre launch.sh"), args.prelaunch_source)

    def test_parse_args_resolves_relative_prelaunch_source(self) -> None:
        args = parse_args(["--task-file", "x.md", "--prelaunch-source", "omo_manager/WORKER_DEFAULTS.md"])
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
        args = parse_args(["--task-file", "x.md", "--tool", "pcodx"])
        self.assertEqual("pcodx", args.tool)
        self.assertTrue(args.tool_explicit)

    def test_parse_args_accepts_is_manager(self) -> None:
        args = parse_args(["--task-file", "x.md", "--is-manager"])
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

    def test_main_success_reminds_to_fill_pending_task_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
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
            self.assertIn("reminder: fill pending_task_items in task frontmatter.", out.getvalue())

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
                "--prompt-file",
                str(prompt),
                "--prelaunch-source",
                str(prelaunch),
                "--dry-run",
            ]
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, main(argv))
            text = out.getvalue()
            self.assertIn(f"prelaunch_source: {prelaunch}", text)
            launch_line = next(line for line in text.splitlines() if "tmux send-keys" in line)
            source_idx = launch_line.index("source ")
            prelaunch_idx = launch_line.index(str(prelaunch))
            export_idx = launch_line.index("export OMO_AGENT_TMUX_TARGET=cfg:DRYRUN")
            exec_idx = launch_line.index("exec bunx @openai/codex")
            self.assertLess(source_idx, prelaunch_idx)
            self.assertLess(prelaunch_idx, export_idx)
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
            with patch('omo_manager.omo_task.tmux') as tmux, patch('omo_manager.omo_task.wait_shell'), patch('omo_manager.omo_task.start_codex') as start_codex_mock:
                tmux.return_value.stdout = 'cfg:7\n'
                self.assertEqual('cfg:7', new_window(args))
            start_codex_mock.assert_called_once_with('cfg:7', args)

    def test_start_codex_sends_command_inside_existing_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), 'x.md', 'cfg', '', 'codex', Path(tmp), 'x', prompt, False, False, '11111111-1111-1111-1111-111111111111', '', ())
            with patch('omo_manager.omo_task.tmux') as tmux, patch('omo_manager.omo_task.wait_command_started') as wait_command_started_mock:
                start_codex('cfg:7', args)
            command = tmux.call_args_list[0].args[0]
            self.assertEqual(['send-keys', '-t', 'cfg:7'], command[:3])
            self.assertIn('bash -lc', command[3])
            self.assertIn('export OMO_AGENT_TMUX_TARGET=cfg:7', command[3])
            self.assertIn('resume 11111111-1111-1111-1111-111111111111', command[3])
            self.assertIn('$(cat --', command[3])
            self.assertEqual('Enter', command[4])
            wait_command_started_mock.assert_called_once_with('cfg:7')

    def test_start_codex_sources_prelaunch_before_worker_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prelaunch = root / "pre launch.sh"
            args = Args(root, "x.md", "cfg", "", "codex", root, "x", prompt, False, False, "", "", (), False, "", prelaunch)
            with patch("omo_manager.omo_task.tmux") as tmux, patch("omo_manager.omo_task.wait_command_started"):
                start_codex("cfg:7", args)
            command = tmux.call_args_list[0].args[0][3]
            source_idx = command.index("source ")
            prelaunch_idx = command.index(str(prelaunch))
            export_idx = command.index("export OMO_AGENT_TMUX_TARGET=cfg:7")
            exec_idx = command.index("exec bunx @openai/codex")
            self.assertLess(source_idx, prelaunch_idx)
            self.assertLess(prelaunch_idx, export_idx)
            self.assertLess(export_idx, exec_idx)

    def test_start_codex_adds_vl_guidance_for_vl_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            args = Args(Path(tmp), "x.md", "vl", "", "pcodx", Path(tmp), "x", prompt, False, False, "", "", ())
            with patch("omo_manager.omo_task.tmux") as tmux, patch("omo_manager.omo_task.wait_command_started"):
                start_codex("vl:7", args)
            command = tmux.call_args_list[0].args[0]
            self.assertIn(str(VL_WORKER_INSTRUCTIONS), command[3])

    def test_start_codex_rejects_context_free_vl_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = Args(Path(tmp), "x.md", "vl", "", "pcodx", Path(tmp), "x", None, False, False, "", "", ())
            with self.assertRaisesRegex(ValueError, "VL launches require --prompt-file"):
                start_codex("vl:7", args)

    def test_wait_command_started_accepts_visible_codex_status(self) -> None:
        with patch('omo_manager.omo_task.tail', return_value=['› Use /skills to list available skills', '  gpt-5.5']), patch('omo_manager.omo_task.current_command', return_value='bash'), patch('omo_manager.omo_task.time.sleep') as sleep:
            wait_command_started('cfg:7')
            sleep.assert_not_called()

    def test_wait_command_started_fails_when_shell_remains_active(self) -> None:
        with patch('omo_manager.omo_task.tail', return_value=[]), patch('omo_manager.omo_task.current_command', return_value='bash'), patch('omo_manager.omo_task.time.monotonic', side_effect=[0, 6]), patch('omo_manager.omo_task.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Codex launch not verified'):
                wait_command_started('cfg:7')

    def test_main_dry_run_does_not_mutate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(0, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--manager-target", "mgr:1", "--workdir", str(root), "--prompt-file", str(prompt), "--dry-run"]))
            self.assertIn("tmux new-window", out.getvalue())
            self.assertIn("tmux send-keys", out.getvalue())
            self.assertIn("export OMO_AGENT_TMUX_TARGET=cfg:DRYRUN", out.getvalue())
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_missing_goal_tree_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("implement manager check\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--manager-target", "mgr:1", "--workdir", str(root), "--prompt-file", str(prompt), "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_new_task_without_runat_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--manager-target", "mgr:1", "--dry-run"]))
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

    def test_main_dry_run_body_runat_does_not_supply_frontmatter_runat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("runat: cfg:2 pcodx\nimplement manager check\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--manager-target", "mgr:1", "--prompt-file", str(prompt), "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_vl_launch_without_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "vl_worker.md", "--tmux-session", "vl", "--workdir", str(root), "--dry-run"]))
            self.assertFalse((root / "vl_worker.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_validates_prompt_file_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--prompt-file", str(root / "missing.md"), "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_rejects_multiline_codex_flag_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--codex-flag", "bad\nflag", "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_rejects_raw_mcp_server_config_without_pcodx_tool(self) -> None:
        args = parse_args(["--task-file", "x.md", "--codex-flag=--config=mcp_servers.pcodx_partial_compact.command=\"bun\""])
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
