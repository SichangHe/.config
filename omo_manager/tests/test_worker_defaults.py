from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from omo_manager.omo_codex_start import Args, prompt_text
from omo_manager.omo_task import DEFAULT_WORKER_INSTRUCTIONS, prompt_input

PB_AGENT_RULE = "- PB agents: continue the task when you can solve a problem. If you encounter a problem you cannot solve, promptly email the human with `email_me.py`. A CAPTCHA that you complete successfully is not a failure and does not require an email."


class WorkerDefaultsTests(unittest.TestCase):
    def test_pb_agent_rule_is_exact(self) -> None:
        defaults = DEFAULT_WORKER_INSTRUCTIONS.read_text(encoding="utf-8")
        pb_rules = [line for line in defaults.splitlines() if "PB agent" in line]
        self.assertEqual([PB_AGENT_RULE], pb_rules)
        self.assertNotIn("`blocked`", pb_rules[0])
        self.assertNotIn("`in-progress`", pb_rules[0])

    def test_task_launcher_prepends_worker_defaults(self) -> None:
        task_prompt = Path("/tmp/worker-prompt.md")
        expression = prompt_input(task_prompt)
        self.assertLess(expression.index(str(DEFAULT_WORKER_INSTRUCTIONS)), expression.index(str(task_prompt)))

    def test_codex_start_prepends_worker_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            task_prompt = root / "worker-prompt.md"
            _ = task_prompt.write_text("worker task\n", encoding="utf-8")
            args = Args(root, "worker.md", "pb:1", "gpt-5.6-sol", "medium", "", task_prompt, 1.0, True, True)
            rendered = prompt_text(args, False)
        defaults = DEFAULT_WORKER_INSTRUCTIONS.read_text(encoding="utf-8").rstrip()
        self.assertTrue(rendered.startswith(f"{defaults}\n\nworker task\n"))


if __name__ == "__main__":
    _ = unittest.main()
