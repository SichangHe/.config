from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml
from omo_manager.omo_manager_rotate import ProcessInfo
from omo_manager.omo_codex_start import (
    Args,
    Pane,
    StartError,
    current_todo_entries,
    has_live_codex_launch,
    launch_command,
    parse_args,
    post_marker_lines,
    prompt_text,
    require_same_shell,
    resolve_pane,
    respawn_codex,
    start,
    validate_task,
    verify_same_pane,
    wait_directory_trust_recovery,
)
from omo_manager.omo_codex_status import Report


class CodexStartTests(unittest.TestCase):
    def args(self, root: Path, **changes: object) -> Args:
        base = Args(
            root=root,
            task_file="worker.md",
            target="cfg:2",
            model="gpt-5.6-terra",
            reasoning_effort="max",
            session_id="019f670b-6a2f-7463-b9be-9aa6ff0cec43",
            prompt_file=None,
            startup_timeout_s=45.0,
            confirm_empty_shell=True,
            dry_run=False,
        )
        return replace(base, **changes)

    def recovery_args(self, root: Path, **changes: object) -> Args:
        return self.args(
            root,
            model="",
            reasoning_effort="",
            session_id="",
            confirm_empty_shell=False,
            confirm_directory_trust=True,
            **changes,
        )

    def trust_prompt(self) -> list[str]:
        return [
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

    def write_task(self, root: Path, *, runat: str = "cfg:2", status: str = "blocked", manager: bool = False) -> None:
        fields = {
            "version": "v1.0.0",
            "status": status,
            "blocked_on": "model capacity" if status == "blocked" else None,
            "runat": runat,
            "tool": "codex",
            "managerat": "cfg:1",
            "is_manager": manager,
            "pending_task_items": [],
        }
        text = "---\n" + yaml.safe_dump({key: value for key, value in fields.items() if value is not None}, sort_keys=False) + "---\n\nGoal.\n"
        (root / "worker.md").write_text(text, encoding="utf-8")
        (root / "TODO.md").write_text(f"current:\n\nworker.md {runat}\n", encoding="utf-8")

    def test_resolve_pane_accepts_exact_window_and_pane_targets(self) -> None:
        result = subprocess.CompletedProcess([], 0, "wl:18.0\t%18\t@18\tzsh\t/tmp\n", "")
        for target in ("wl:18", "wl:18.0"):
            with self.subTest(target=target), patch("omo_manager.omo_codex_start.run", return_value=result):
                self.assertEqual(Pane("wl:18.0", "%18", "@18", "zsh", Path("/tmp")), resolve_pane(target))

    def test_resolve_pane_rejects_ambiguous_identity_fallbacks(self) -> None:
        mismatches = (("wl:18", "wl:1.0"), ("wl:18", "other:18.0"), ("wl:18.1", "wl:18.0"))
        for requested, resolved in mismatches:
            with self.subTest(requested=requested, resolved=resolved):
                result = subprocess.CompletedProcess([], 0, f"{resolved}\t%18\t@18\tzsh\t/tmp\n", "")
                with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "does not exist exactly"):
                    resolve_pane(requested)

    def test_resolve_pane_reports_empty_tmux_expansion_as_missing_target(self) -> None:
        result = subprocess.CompletedProcess([], 0, ":.\t\t\t\t\n", "")
        with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "target does not exist: wl:18"):
            resolve_pane("wl:18")

    def test_resolve_pane_rejects_near_empty_tmux_expansion(self) -> None:
        result = subprocess.CompletedProcess([], 0, ":.\t\t\t\t\n\n", "")
        with patch("omo_manager.omo_codex_start.run", return_value=result), self.assertRaisesRegex(StartError, "invalid identity"):
            resolve_pane("wl:18")

    def test_validate_task_requires_active_exact_todo_and_same_pane(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                self.assertFalse(validate_task(self.args(root), pane))
            (root / "TODO.md").write_text("current:\n", encoding="utf-8")
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane):
                with self.assertRaisesRegex(StartError, "TODO `current`"):
                    validate_task(self.args(root), pane)

    def test_current_todo_entries_excludes_other_sections(self) -> None:
        text = "current:\nactive.md cfg:1\nhuman pending:\nhuman.md cfg:2\nprevious:\nold.md cfg:3\n"
        self.assertEqual({"active.md cfg:1"}, current_todo_entries(text))

    def test_validate_task_rejects_done_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, status="done")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with self.assertRaisesRegex(StartError, "not active"):
                validate_task(self.args(root), pane)

    def test_resume_command_preserves_target_model_effort_and_session(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
        command = launch_command(self.args(root), pane, None, "[marker]")
        self.assertIn("OMO_AGENT_TMUX_TARGET=cfg:2.0", command)
        self.assertIn("--model gpt-5.6-terra", command)
        self.assertIn("model_reasoning_effort=", command)
        self.assertIn("resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)
        self.assertIn("cd '/tmp/work logs'", command)
        self.assertIn("printf '%s\\n' '[marker]'", command)

    def test_fresh_manager_prompt_includes_defaults_manager_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            prompt = root / "prompt.txt"
            prompt.write_text("task prompt\n", encoding="utf-8")
            (root / "MANAGER.md").write_text("manager instructions\n", encoding="utf-8")
            with patch("omo_manager.omo_codex_start.WORKER_DEFAULTS", prompt):
                text = prompt_text(self.args(root, session_id="", prompt_file=prompt), True)
            self.assertEqual("task prompt\n\nmanager instructions\n\ntask prompt\n", text)

    def test_fresh_command_quotes_prompt_substitution_as_one_argument(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
        prompt_path = Path("/tmp/prompt with spaces.txt")
        command = launch_command(self.args(root, session_id="", prompt_file=prompt_path), pane, prompt_path, "[marker]")
        self.assertIn('"$(cat -- \'/tmp/prompt with spaces.txt\')"', command)

    def test_restart_running_needs_no_session_or_shell_confirmation(self) -> None:
        args = parse_args(
            [
                "--task-file",
                "worker.md",
                "--target",
                "cfg:2",
                "--model",
                "gpt-5.6-terra",
                "--reasoning-effort",
                "max",
                "--restart-running",
            ]
        )
        self.assertTrue(args.restart_running)
        self.assertEqual("", args.session_id)

    def test_restart_running_rejects_caller_supplied_session(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--task-file",
                    "worker.md",
                    "--target",
                    "cfg:2",
                    "--model",
                    "gpt-5.6-terra",
                    "--reasoning-effort",
                    "max",
                    "--restart-running",
                    "--session-id",
                    "019f670b-6a2f-7463-b9be-9aa6ff0cec43",
                ]
            )

    def test_directory_trust_recovery_accepts_only_its_exact_argument_set(self) -> None:
        args = parse_args(
            [
                "--root",
                "/tmp/work-logs",
                "--task-file",
                "worker.md",
                "--target",
                "cfg:2.0",
                "--confirm-directory-trust",
                "--startup-timeout-s",
                "3",
                "--dry-run",
            ]
        )
        self.assertTrue(args.confirm_directory_trust)
        self.assertEqual("", args.model)
        self.assertEqual("", args.reasoning_effort)
        self.assertEqual(3.0, args.startup_timeout_s)
        self.assertTrue(args.dry_run)

        conflicts = (
            ("--model", "gpt-5.6-terra"),
            ("--reasoning-effort", "max"),
            ("--session-id", "019f670b-6a2f-7463-b9be-9aa6ff0cec43"),
            ("--prompt-file", "/tmp/prompt"),
            ("--confirm-empty-shell",),
            ("--restart-running",),
        )
        base = ["--task-file", "worker.md", "--target", "cfg:2", "--confirm-directory-trust"]
        for conflict in conflicts:
            with self.subTest(conflict=conflict), self.assertRaises(SystemExit):
                parse_args([*base, *conflict])
        with self.assertRaises(SystemExit):
            parse_args(["--task-file", "worker.md", "--target", "cfg:2"])

    def test_parse_args_preserves_environment_root_default(self) -> None:
        with patch.dict("os.environ", {"OMO_WORK_LOGS_ROOT": "/tmp/environment-work-logs"}, clear=True):
            args = parse_args(["--task-file", "worker.md", "--target", "cfg:2", "--confirm-directory-trust"])
        self.assertEqual(Path("/tmp/environment-work-logs"), args.root)

    def test_restart_command_execs_resumed_session(self) -> None:
        root = Path("/tmp/work logs")
        pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
        command = launch_command(self.args(root, restart_running=True), pane, None, "[marker]", replace_process=True)
        self.assertIn("&& exec bunx", command)
        self.assertIn("resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)

    def test_respawn_replaces_process_and_preserves_pane_identity(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp/work logs"))
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch("omo_manager.omo_codex_start.run", return_value=completed) as run, patch("omo_manager.omo_codex_start.verify_same_pane") as verify:
            respawn_codex(pane, "exec codex resume session")
        run.assert_called_once_with(["tmux", "respawn-pane", "-k", "-t", "%2", "-c", "/tmp/work logs", "exec codex resume session"])
        verify.assert_called_once_with(pane)

    def test_restart_captures_session_before_atomic_respawn(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
            args = self.args(root, session_id="", restart_running=True)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("019f670b-6a2f-7463-b9be-9aa6ff0cec43", "")) as capture,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                patch("omo_manager.omo_codex_start.wait_started", return_value="running"),
            ):
                self.assertEqual("running", start(args))
            capture.assert_called_once_with("%2", 240, 10.0)
            command = respawn.call_args.args[1]
            self.assertIn("resume 019f670b-6a2f-7463-b9be-9aa6ff0cec43", command)

    def test_restart_does_not_replace_process_when_session_capture_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("running", ["working"])),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("", "")),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "pane was not replaced"),
            ):
                start(self.args(root, session_id="", restart_running=True))
            respawn.assert_not_called()

    def test_restart_rejects_non_codex_process_before_session_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "python", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", return_value=Report("not_codex", ["python output"])),
                patch("omo_manager.omo_codex_start.query_status_session_id") as capture,
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "not a supported live Codex pane"),
            ):
                start(self.args(root, session_id="", restart_running=True))
            capture.assert_not_called()
            respawn.assert_not_called()

    def test_restart_rejects_process_transition_after_session_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bun", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.inspect", side_effect=[Report("running", ["working"]), Report("not_codex", ["shell"])]),
                patch("omo_manager.omo_codex_start.query_status_session_id", return_value=("019f670b-6a2f-7463-b9be-9aa6ff0cec43", "")),
                patch("omo_manager.omo_codex_start.respawn_codex") as respawn,
                self.assertRaisesRegex(StartError, "not a supported live Codex pane"),
            ):
                start(self.args(root, session_id="", restart_running=True))
            respawn.assert_not_called()

    def test_validate_task_rejects_non_codex_tool(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            path = root / "worker.md"
            path.write_text(path.read_text(encoding="utf-8").replace("tool: codex", "tool: pcodx"), encoding="utf-8")
            pane = Pane("cfg:2.0", "%2", "@2", "zsh", root)
            with self.assertRaisesRegex(StartError, "only `tool: codex`"):
                validate_task(self.args(root), pane)

    def test_post_marker_capture_uses_numeric_target_not_pane_id(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "zsh", Path("/tmp"))
        with patch("omo_manager.omo_codex_start.tail", return_value=["old", "[marker]", "new"]) as capture:
            self.assertEqual(["new"], post_marker_lines(pane, "[marker]"))
        capture.assert_called_once_with("cfg:2.0", 200)

    def test_require_same_shell_rejects_codex_after_lock_wait(self) -> None:
        expected = Pane("cfg:2.0", "%2", "@2", "zsh", Path("/tmp"))
        running = Pane("cfg:2.0", "%2", "@2", "bun", Path("/tmp"))
        with patch("omo_manager.omo_codex_start.resolve_pane", return_value=running):
            with self.assertRaisesRegex(StartError, "not an empty shell"):
                require_same_shell(expected)

    def test_start_rejects_human_owned_session_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            pane = Pane("hcfg:2.0", "%2", "@2", "zsh", root)
            with patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane), patch(
                "omo_manager.omo_codex_start.require_same_shell"
            ) as require_shell, self.assertRaisesRegex(StartError, "human-owned"):
                start(self.args(root, target="hcfg:2"))

            require_shell.assert_not_called()

    def test_directory_trust_recovery_sends_one_enter_to_pinned_pane(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch(
                    "omo_manager.omo_codex_start.capture_pane",
                    side_effect=[self.trust_prompt(), self.trust_prompt(), ["ready"]],
                ) as capture,
                patch("omo_manager.omo_codex_start.has_live_codex_launch", return_value=True) as process,
                patch("omo_manager.omo_codex_start.classify_status", return_value="ready"),
                patch("omo_manager.omo_codex_start.run", return_value=completed) as run,
            ):
                self.assertEqual("ready", start(self.recovery_args(root)))
            self.assertEqual(3, capture.call_count)
            self.assertEqual(2, process.call_count)
            run.assert_called_once_with(["tmux", "send-keys", "-t", "%2", "Enter"])

    def test_directory_trust_dry_run_checks_without_input_or_wait(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.capture_pane", return_value=self.trust_prompt()) as capture,
                patch("omo_manager.omo_codex_start.has_live_codex_launch", return_value=True) as process,
                patch("omo_manager.omo_codex_start.classify_status") as classify,
                patch("omo_manager.omo_codex_start.run") as run,
                patch("builtins.print") as output,
            ):
                self.assertEqual("dry-run", start(self.recovery_args(root, dry_run=True)))
            capture.assert_called_once_with("%2", 200)
            process.assert_called_once_with("%2")
            classify.assert_not_called()
            run.assert_not_called()
            output.assert_any_call("target: cfg:2.0")
            output.assert_any_call("mode: confirm-directory-trust")

    def test_directory_trust_recovery_rejects_human_and_caller_panes_before_capture(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            cases = (
                (Pane("hcfg:2.0", "%2", "@2", "bunx", root), {}, "human-owned"),
                (Pane("cfg:2.0", "%2", "@2", "bunx", root), {"TMUX_PANE": "%2"}, "different pane"),
            )
            for pane, environment, message in cases:
                with (
                    self.subTest(message=message),
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch.dict("os.environ", environment, clear=True),
                    patch("omo_manager.omo_codex_start.capture_pane") as capture,
                    patch("omo_manager.omo_codex_start.run") as run,
                    self.assertRaisesRegex(StartError, message),
                ):
                    start(self.recovery_args(root, target=pane.target))
                capture.assert_not_called()
                run.assert_not_called()

    def test_directory_trust_recovery_rejects_task_runat_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root, runat="cfg:3")
            target = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            other = Pane("cfg:3.0", "%3", "@3", "bunx", root)

            def resolve(name: str) -> Pane:
                return other if name == "cfg:3" else target

            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.capture_pane") as capture,
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "does not identify target"),
            ):
                start(self.recovery_args(root))
            capture.assert_not_called()
            run.assert_not_called()

    def test_directory_trust_recovery_rejects_unsafe_or_stale_frames(self) -> None:
        prompt = self.trust_prompt()
        cases = {
            "partial": prompt[:3],
            "reordered": [*prompt[:7], prompt[8], prompt[7], *prompt[9:]],
            "changed choice": [*prompt[:7], "› 2. No, quit", *prompt[8:]],
            "stale scrollback": [*prompt, "", "ordinary shell output"],
            "ordinary non-Codex": ["$ echo Press enter to continue"],
        }
        for name, lines in cases.items():
            with tempfile.TemporaryDirectory() as raw_root:
                root = Path(raw_root)
                self.write_task(root)
                pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
                with (
                    self.subTest(name=name),
                    patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                    patch("omo_manager.omo_codex_start.capture_pane", return_value=lines),
                    patch("omo_manager.omo_codex_start.has_live_codex_launch") as process,
                    patch("omo_manager.omo_codex_start.run") as run,
                    self.assertRaisesRegex(StartError, "exact Codex directory-trust prompt"),
                ):
                    start(self.recovery_args(root))
                process.assert_not_called()
                run.assert_not_called()

    def test_directory_trust_recovery_rechecks_prompt_before_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            changed = [*self.trust_prompt(), "", "new output"]
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.capture_pane", side_effect=[self.trust_prompt(), changed]),
                patch("omo_manager.omo_codex_start.has_live_codex_launch", return_value=True) as process,
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "exact Codex directory-trust prompt"),
            ):
                start(self.recovery_args(root))
            process.assert_called_once_with("%2")
            run.assert_not_called()

    def test_directory_trust_recovery_requires_one_live_expected_process(self) -> None:
        pane_process = ProcessInfo(100, 1, "S", ("zsh",))
        launch_argv = ("/usr/bin/bunx", "@openai/codex", "--model", "gpt-5.6-sol")
        cases = {
            "missing": {100: pane_process},
            "wrong argv": {100: pane_process, 101: ProcessInfo(101, 100, "S", ("/usr/bin/bunx", "other-package"))},
            "dead": {100: pane_process, 101: ProcessInfo(101, 100, "Z", launch_argv)},
            "multiple": {
                100: pane_process,
                101: ProcessInfo(101, 100, "S", launch_argv),
                102: ProcessInfo(102, 100, "S", launch_argv),
            },
        }
        pane_pid = subprocess.CompletedProcess([], 0, "100\n", "")
        for process_state, processes in cases.items():
            with (
                self.subTest(process_state=process_state),
                patch("omo_manager.omo_task.tmux", return_value=pane_pid),
                patch("omo_manager.omo_task.read_processes", return_value=processes),
            ):
                self.assertFalse(has_live_codex_launch("%2"))

        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "python", root)
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch("omo_manager.omo_codex_start.capture_pane", return_value=self.trust_prompt()),
                patch("omo_manager.omo_codex_start.has_live_codex_launch", return_value=False),
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "exactly one live Codex launch process"),
            ):
                start(self.recovery_args(root))
            run.assert_not_called()

    def test_directory_trust_recovery_rechecks_identity_after_final_process_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            moved = Pane("cfg:2.0", "%2", "@3", "bunx", root)
            process_calls = 0
            pane_moved = False

            def process(_: str) -> bool:
                nonlocal pane_moved, process_calls
                process_calls += 1
                pane_moved = process_calls == 2
                return True

            def resolve(_: str) -> Pane:
                return moved if pane_moved else pane

            with (
                patch("omo_manager.omo_codex_start.resolve_pane", side_effect=resolve),
                patch("omo_manager.omo_codex_start.capture_pane", return_value=self.trust_prompt()),
                patch("omo_manager.omo_codex_start.has_live_codex_launch", side_effect=process),
                patch("omo_manager.omo_codex_start.run") as run,
                self.assertRaisesRegex(StartError, "pane or window identity changed"),
            ):
                start(self.recovery_args(root))
            self.assertEqual(2, process_calls)
            run.assert_not_called()

    def test_verify_same_pane_rejects_pane_or_window_replacement(self) -> None:
        expected = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
        replacements = (
            Pane("cfg:2.0", "%3", "@2", "bunx", Path("/tmp")),
            Pane("cfg:2.0", "%2", "@3", "bunx", Path("/tmp")),
        )
        for replacement in replacements:
            with (
                self.subTest(replacement=replacement),
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=replacement),
                self.assertRaisesRegex(StartError, "pane or window identity changed"),
            ):
                verify_same_pane(expected)

    def test_directory_trust_wait_accepts_only_ready_or_running(self) -> None:
        pane = Pane("cfg:2.0", "%2", "@2", "bunx", Path("/tmp"))
        for success in ("ready", "running"):
            with (
                self.subTest(success=success),
                patch("omo_manager.omo_codex_start.verify_same_pane"),
                patch("omo_manager.omo_codex_start.capture_pane", return_value=["screen"]),
                patch("omo_manager.omo_codex_start.classify_status", return_value=success),
            ):
                self.assertEqual(success, wait_directory_trust_recovery(pane, 1.0))

        for failure in ("not_codex", "stuck_input", "waiting_subagent", "missing", "unknown"):
            with (
                self.subTest(failure=failure),
                patch("omo_manager.omo_codex_start.verify_same_pane"),
                patch("omo_manager.omo_codex_start.capture_pane", return_value=["screen"]),
                patch("omo_manager.omo_codex_start.classify_status", return_value=failure),
                patch("omo_manager.omo_codex_start.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
                patch("omo_manager.omo_codex_start.time.sleep"),
                self.assertRaisesRegex(StartError, "timed out"),
            ):
                wait_directory_trust_recovery(pane, 1.0)

        with (
            patch("omo_manager.omo_codex_start.verify_same_pane"),
            patch("omo_manager.omo_codex_start.capture_pane", return_value=["screen"]),
            patch("omo_manager.omo_codex_start.classify_status", return_value="error"),
            self.assertRaisesRegex(StartError, "error state"),
        ):
            wait_directory_trust_recovery(pane, 1.0)

    def test_directory_trust_timeout_does_not_send_a_second_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            self.write_task(root)
            pane = Pane("cfg:2.0", "%2", "@2", "bunx", root)
            completed = subprocess.CompletedProcess([], 0, "", "")
            with (
                patch("omo_manager.omo_codex_start.resolve_pane", return_value=pane),
                patch(
                    "omo_manager.omo_codex_start.capture_pane",
                    side_effect=[self.trust_prompt(), self.trust_prompt(), ["still waiting"]],
                ),
                patch("omo_manager.omo_codex_start.has_live_codex_launch", return_value=True),
                patch("omo_manager.omo_codex_start.classify_status", return_value="not_codex"),
                patch("omo_manager.omo_codex_start.time.monotonic", side_effect=[0.0, 0.0, 2.0]),
                patch("omo_manager.omo_codex_start.time.sleep"),
                patch("omo_manager.omo_codex_start.run", return_value=completed) as run,
                self.assertRaisesRegex(StartError, "timed out"),
            ):
                start(self.recovery_args(root, startup_timeout_s=1.0))
            run.assert_called_once_with(["tmux", "send-keys", "-t", "%2", "Enter"])


if __name__ == "__main__":
    unittest.main()
