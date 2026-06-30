import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_task import Args, DEFAULT_TOOL, DEFAULT_WORKER_INSTRUCTIONS, PCODX_WRAPPER, PENDING_TASK_ITEMS_MARKER, VL_WORKER_INSTRUCTIONS, codex_cmd, effective_tool, ensure_task_file, is_vl_agent, link_todo, main, new_window, parse_args, runat_goal_tree_error, start_codex, validate_inputs, validate_runat_goal_tree, wait_command_started


VALID_GOAL_TREE = "implement manager check\n- reject missing task goal tree\n"


class OmoTaskTests(unittest.TestCase):
    def test_creates_task_file_with_runat_header_and_todo_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, 'x.md', 'cfg', '2', 'codex', None, '', prompt, False, False, '', '', ())
            path = ensure_task_file(args, 'cfg:2')
            link_todo(args, 'cfg:2')
            self.assertEqual('runat: cfg:2 codex', path.read_text(encoding='utf-8').splitlines()[0])
            self.assertIn(PENDING_TASK_ITEMS_MARKER, path.read_text(encoding="utf-8"))
            self.assertIn('x.md cfg:2', (root / 'TODO.md').read_text(encoding='utf-8'))

    def test_absolute_task_file_writes_relative_todo_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "nested" / "x.md"
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, str(task), "cfg", "2", "codex", None, "", prompt, False, False, "", "", ())
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
            args = Args(root, 'x.md', 'cfg', '2', 'pcodx', None, '', prompt, False, False, '', '', ())
            path = ensure_task_file(args, 'cfg:2')
            self.assertEqual('runat: cfg:2 pcodx', path.read_text(encoding='utf-8').splitlines()[0])

    def test_validates_runat_goal_tree(self) -> None:
        validate_runat_goal_tree("runat: cfg:2 pcodx\nimplement manager check\n- reject missing task goal tree\n")
        validate_runat_goal_tree("runat: cfg:2 pcodx\nmanagerat: wl:1\nimplement manager check\n- reject missing task goal tree\n")
        self.assertIn("high-level goal", runat_goal_tree_error("runat: cfg:2 pcodx\n\n- reject missing task goal tree\n"))
        self.assertIn("plain high-level", runat_goal_tree_error("runat: cfg:2 pcodx\n- implement manager check\n- reject missing task goal tree\n"))
        self.assertIn("concrete bullet subgoal", runat_goal_tree_error("runat: cfg:2 pcodx\nimplement manager check\n"))
        self.assertIn("concrete bullet subgoal", runat_goal_tree_error("runat:\tcfg:2 pcodx\nimplement manager check\n"))

    def test_new_task_file_writes_managerat_before_pending_items_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(VALID_GOAL_TREE, encoding="utf-8")
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", (), False, "wl:1")
            path = ensure_task_file(args, "cfg:2")
            self.assertEqual(
                "runat: cfg:2 codex\nmanagerat: wl:1\nimplement manager check\n- reject missing task goal tree\n(above are pending task items)\n",
                path.read_text(encoding="utf-8"),
            )

    def test_prompt_mentioning_pending_marker_still_gets_marker_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text(
                "review task boilerplate\n"
                "- check whether any created task is missing `(above are pending task items)`\n",
                encoding="utf-8",
            )
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", ())
            path = ensure_task_file(args, "cfg:2")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, lines.count(PENDING_TASK_ITEMS_MARKER))
            self.assertEqual(PENDING_TASK_ITEMS_MARKER, lines[-1])

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
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", ())
            path = ensure_task_file(args, "cfg:2")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, lines.count(PENDING_TASK_ITEMS_MARKER))
            self.assertEqual("- keep this human item pending", lines[-1])

    def test_new_task_file_places_pending_marker_before_context_bullets(self) -> None:
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
            args = Args(root, "x.md", "cfg", "2", "codex", None, "", prompt, False, False, "", "", ())
            path = ensure_task_file(args, "cfg:2")
            self.assertEqual(
                "runat: cfg:2 codex\n"
                "route cleanup\n"
                "- preserve human wording\n"
                "(above are pending task items)\n"
                "\n"
                "Human sources:\n"
                "\n"
                "- manager_mail/8649.txt\n",
                path.read_text(encoding="utf-8"),
            )

    def test_new_task_file_requires_goal_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = Args(root, "x.md", "cfg", "2", "pcodx", None, "", None, False, False, "", "", ())
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

    def test_parse_args_accepts_repeatable_codex_flags(self) -> None:
        args = parse_args(["--task-file", "x.md", "--reasoning-effort", "xhigh", "--codex-flag=--profile", "--codex-flag", "deep-review"])
        self.assertEqual("xhigh", args.reasoning_effort)
        self.assertEqual(("--profile", "deep-review"), args.codex_flags)
        self.assertEqual(DEFAULT_TOOL, args.tool)

    def test_parse_args_accepts_pcodx_tool(self) -> None:
        args = parse_args(["--task-file", "x.md", "--tool", "pcodx"])
        self.assertEqual("pcodx", args.tool)
        self.assertTrue(args.tool_explicit)

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
                self.assertEqual(0, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--prompt-file", str(prompt), "--dry-run"]))
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
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--tmux-session", "cfg", "--workdir", str(root), "--prompt-file", str(prompt), "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_allows_new_task_without_runat_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, main(["--root", str(root), "--task-file", "x.md", "--dry-run"]))
            self.assertFalse((root / "x.md").exists())
            self.assertFalse((root / "TODO.md").exists())

    def test_main_dry_run_validates_prompt_started_runat_without_tmux_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt = root / "prompt.md"
            prompt.write_text("runat: cfg:2 pcodx\nimplement manager check\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, main(["--root", str(root), "--task-file", "x.md", "--prompt-file", str(prompt), "--dry-run"]))
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
        args = parse_args(["--task-file", "x.md", "--tool", "pcodx", "--codex-flag=--config=mcp_servers.pcodx_partial_compact.command=\"bun\""])
        validate_inputs(args)

    def test_rejects_non_codex_tool(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--task-file", "x.md", "--tool", "other"])


if __name__ == '__main__':
    _ = unittest.main()
