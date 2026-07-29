from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from omo_manager.omo_codex_start import Args, Pane, StartError, current_todo_entries, launch_command, post_marker_lines, prompt_text, require_same_shell, resolve_pane, start, validate_task


class CodexStartTests(unittest.TestCase):
    def args(self, root: Path, **changes: object) -> Args:
        values: dict[str, object] = {
            "root": root,
            "task_file": "worker.md",
            "target": "cfg:2",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "max",
            "session_id": "019f670b-6a2f-7463-b9be-9aa6ff0cec43",
            "prompt_file": None,
            "startup_timeout_s": 45.0,
            "confirm_empty_shell": True,
            "dry_run": False,
        }
        values.update(changes)
        return Args(**values)  # type: ignore[arg-type]

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


if __name__ == "__main__":
    unittest.main()
