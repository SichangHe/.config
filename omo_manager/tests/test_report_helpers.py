from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


OMO_DIR = Path.home() / ".config/omo_manager"


def task_frontmatter(*, runat: str = "cfg:7", managerat: str = "main:0.0") -> str:
    return "\n".join(
        [
            "---",
            "version: v1.0.0",
            "status: running",
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            "is_manager: false",
            "pending_task_items: []",
            "---",
            "",
        ]
    )


def dated_manager_file(root: Path) -> Path:
    return root / f"work_manager_{datetime.now().astimezone().strftime('%Y-%m-%d')}.md"


def write_fake_tmux(bin_dir: Path, *, session: str = "cfg", window: str = "7", pane: str = "0") -> None:
    tmux = bin_dir / "tmux"
    tmux.write_text(
        f"#!/usr/bin/env bash\nprintf '{session}\\t{window}\\t{pane}\\t%%1701\\tworker\\n'\n",
        encoding="utf-8",
    )
    tmux.chmod(0o700)


class ReportHelperTests(unittest.TestCase):
    def test_omo_report_infers_root_and_task_from_worker_tmux_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir, pane="1")
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:7\n", encoding="utf-8")
            (root / "task.md").write_text(task_frontmatter(), encoding="utf-8")
            env = {
                **os.environ,
                "OMO_MANAGER_LOCAL_ENV": str(local_env),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMUX_PANE": "%1701",
            }

            alloc = subprocess.run(
                [str(OMO_DIR / "omo_report.sh"), "--alloc-message-file"],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual("", alloc.stderr)
            self.assertEqual(0, alloc.returncode)
            report_file = Path(alloc.stdout.strip())
            self.assertTrue(report_file.name.startswith("task."))
            report_file.write_text("default report\n", encoding="utf-8")

            submit = subprocess.run(
                [str(OMO_DIR / "omo_report.sh"), "--status", "done", "--agent", "agent-default", "--message-file", str(report_file)],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", submit.stderr)
            self.assertEqual(0, submit.returncode)
            manager_text = dated_manager_file(root).read_text(encoding="utf-8")
            report_path_match = re.search(r"^\(from agent cfg:7\.1 (/tmp/[^)]+)\)$", manager_text, flags=re.MULTILINE)
            self.assertIsNotNone(report_path_match)
            assert report_path_match is not None
            durable_text = Path(report_path_match.group(1)).read_text(encoding="utf-8")
            self.assertIn("task-file=task.md", durable_text)
            self.assertIn("default report\n", durable_text)

    def test_omo_report_inference_failure_names_explicit_task_file_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir, window="9")
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:7\n", encoding="utf-8")
            (root / "task.md").write_text(task_frontmatter(runat="cfg:7"), encoding="utf-8")

            result = subprocess.run(
                [str(OMO_DIR / "omo_report.sh"), "--alloc-message-file"],
                cwd=tmp,
                env={
                    **os.environ,
                    "OMO_MANAGER_LOCAL_ENV": str(local_env),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("could not infer task file for tmux target cfg:9.0", result.stderr)
            self.assertIn("pass --task-file explicitly", result.stderr)

    def test_omo_report_does_not_infer_different_explicit_pane_in_same_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir, pane="1")
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nother.md cfg:7.2\n", encoding="utf-8")
            (root / "other.md").write_text(task_frontmatter(runat="cfg:7.2"), encoding="utf-8")

            result = subprocess.run(
                [str(OMO_DIR / "omo_report.sh"), "--alloc-message-file"],
                cwd=tmp,
                env={
                    **os.environ,
                    "OMO_MANAGER_LOCAL_ENV": str(local_env),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(2, result.returncode)
            self.assertIn("could not infer task file for tmux target cfg:7.1", result.stderr)

    def test_omo_dispatch_report_instruction_uses_default_report_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            bin_dir = home / ".config/bin"
            bin_dir.mkdir(parents=True)
            capture = tmp_path / "captured-prompt.txt"
            tmux_send = bin_dir / "omo_tmux_send.py"
            tmux_send.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
message_file=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --message-file) message_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
prompt_file=$(sed -n 's/^Read the dispatch prompt from \\(.*\\) and follow it exactly\\.$/\\1/p' "$message_file")
cp "$prompt_file" "$OMO_CAPTURE_PROMPT"
""",
                encoding="utf-8",
            )
            tmux_send.chmod(0o700)
            (root / "task.md").write_text(
                "Do the task.\n(for manager: ask agent to report back to manager)\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    str(OMO_DIR / "omo_dispatch.sh"),
                    "--root",
                    str(root),
                    "--file",
                    "task.md",
                    "--start",
                    "1",
                    "--end",
                    "2",
                    "--tmux-target",
                    "cfg:7",
                    "--no-submit",
                ],
                cwd=tmp,
                env={**os.environ, "HOME": str(home), "OMO_CAPTURE_PROMPT": str(capture)},
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            prompt = capture.read_text(encoding="utf-8")
            self.assertIn("REPORT_FILE=$(omo_report.sh --alloc-message-file)", prompt)
            self.assertIn('omo_report.sh --status STATUS --agent agent-name --message-file "$REPORT_FILE"', prompt)
            self.assertIn("REPORT_FILE=$(omo_report.sh --task-file task.md --alloc-message-file)", prompt)
            self.assertIn('omo_report.sh --task-file task.md --status STATUS --agent agent-name --message-file "$REPORT_FILE"', prompt)
            self.assertIn("--root", prompt)
            self.assertIn("editor/file-editing tool", prompt)
            self.assertNotIn("omo_report.sh --root", prompt)
            self.assertNotIn("omo_report.sh --root ", prompt)
            self.assertNotRegex(prompt.lower(), re.compile(r"\bcat\b|cat\s*>"))


if __name__ == "__main__":
    unittest.main()
