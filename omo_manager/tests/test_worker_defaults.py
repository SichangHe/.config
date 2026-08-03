from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from omo_manager.omo_codex_start import Args as StartArgs
from omo_manager.omo_codex_start import WORKER_DEFAULTS, prompt_text
from omo_manager.omo_task import DEFAULT_WORKER_INSTRUCTIONS, prompt_input


HARD_STOP_POLICY = "When any browser or search result shows a robot/CAPTCHA challenge, consent/authentication wall, unusual-traffic page, or equivalent human-only hard stop, stop retrying that source and immediately send one redacted manager report: allocate a private report file with `omo_report.sh --alloc-message-file`, write it, then submit with `omo_report.sh --status STATUS --message-file FILE`. Use `blocked` only when the task cannot continue; otherwise use `in-progress` and name the safe fallback. Do not include challenge-page URLs, account details, cookies, tokens, or other sensitive state. The final report may refer to this immediate alert without duplicating it."


class WorkerDefaultsTests(unittest.TestCase):
    def test_common_defaults_contain_exact_hard_stop_policy_once(self) -> None:
        defaults = DEFAULT_WORKER_INSTRUCTIONS.read_text(encoding="utf-8")
        self.assertEqual(1, defaults.count(HARD_STOP_POLICY))

    def test_fresh_task_launch_injects_common_defaults(self) -> None:
        expression = prompt_input(None)
        rendered = subprocess.run(
            ["bash", "-c", f'set -- {expression}; printf %s "$1"'],
            check=True,
            capture_output=True,
        ).stdout
        defaults = DEFAULT_WORKER_INSTRUCTIONS.read_bytes().rstrip(b"\n")
        self.assertEqual(defaults, rendered)
        self.assertIn(HARD_STOP_POLICY.encode(), rendered)

    def test_fresh_direct_codex_launch_injects_common_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            task_prompt = root / "prompt.md"
            task_prompt.write_text("task prompt\n", encoding="utf-8")
            args = StartArgs(
                root=root,
                task_file="worker.md",
                target="cfg:2",
                model="gpt-5.6-terra",
                reasoning_effort="max",
                session_id="",
                prompt_file=task_prompt,
                startup_timeout_s=45.0,
                confirm_empty_shell=True,
                dry_run=True,
            )
            rendered = prompt_text(args, False)
        defaults = WORKER_DEFAULTS.read_text(encoding="utf-8").rstrip()
        self.assertEqual(f"{defaults}\n\ntask prompt\n", rendered)
        self.assertIn(HARD_STOP_POLICY, rendered)


if __name__ == "__main__":
    unittest.main()
