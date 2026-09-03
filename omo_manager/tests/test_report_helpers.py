from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


OMO_DIR = Path.home() / ".config/omo_manager"


def task_frontmatter(*, runat: str = "cfg:7", managerat: str = "main:0.0", is_manager: bool = False, status: str = "running") -> str:
    return "\n".join(
        [
            "---",
            "version: v1.0.0",
            f"status: {status}",
            f"runat: {runat}",
            "tool: codex",
            f"managerat: {managerat}",
            f"is_manager: {str(is_manager).lower()}",
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
    def test_omo_report_large_snapshot_survives_successful_early_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir)
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:7\n", encoding="utf-8")
            (root / "task.md").write_text(task_frontmatter(), encoding="utf-8")
            helper = tmp_path / "omo_report.sh"
            helper.write_bytes((OMO_DIR / "omo_report.sh").read_bytes() + b"\n" + b"# padding\n" * 200_000)
            helper.chmod(0o700)

            result = subprocess.run(
                [str(helper), "--alloc-message-file"],
                cwd=tmp,
                env={
                    **os.environ,
                    "OMO_MANAGER_LOCAL_ENV": str(local_env),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                    "XDG_STATE_HOME": str(tmp_path / "state"),
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            report_file = Path(result.stdout.strip())
            self.assertTrue(report_file.name.startswith("task."))
            report_file.unlink()

    def test_omo_report_infers_root_and_task_from_worker_tmux_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir)
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:7\n", encoding="utf-8")
            (root / "task.md").write_text(task_frontmatter(), encoding="utf-8")
            env = {
                **os.environ,
                "OMO_MANAGER_LOCAL_ENV": str(local_env),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMUX_PANE": "%1701",
                "XDG_STATE_HOME": str(tmp_path / "state"),
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
            report_path_match = re.search(r"^\(from agent cfg:7 (/tmp/[^)]+)\)$", manager_text, flags=re.MULTILINE)
            self.assertIsNotNone(report_path_match)
            assert report_path_match is not None
            durable_text = Path(report_path_match.group(1)).read_text(encoding="utf-8")
            self.assertIn("task-file=task.md", durable_text)
            self.assertIn("default report\n", durable_text)

    def test_omo_report_falls_back_to_legacy_home_task_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            home = tmp_path / "home"
            legacy_root = home / "work_logs"
            legacy_root.mkdir(parents=True)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir)
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n", encoding="utf-8")
            (legacy_root / "TODO.md").write_text("current:\nlegacy.md cfg:7\n", encoding="utf-8")
            (legacy_root / "legacy.md").write_text(task_frontmatter(), encoding="utf-8")
            env = {
                **os.environ,
                "HOME": str(home),
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

            self.assertEqual(0, alloc.returncode, alloc.stderr)
            report_file = Path(alloc.stdout.strip())
            self.assertTrue(report_file.name.startswith("legacy."))
            report_file.unlink()

    def test_omo_report_prefers_single_current_task_over_stale_human_pending_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir)
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            (root / "TODO.md").write_text(
                "current:\ncurrent.md cfg:7\n\nhuman pending:\nstale.md\n",
                encoding="utf-8",
            )
            _ = (root / "current.md").write_text(task_frontmatter(), encoding="utf-8")
            _ = (root / "stale.md").write_text(task_frontmatter(), encoding="utf-8")

            result = subprocess.run(
                [str(OMO_DIR / "omo_report.sh"), "--alloc-message-file"],
                cwd=tmp,
                env={
                    **os.environ,
                    "OMO_MANAGER_LOCAL_ENV": str(local_env),
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "TMUX_PANE": "%1701",
                    "XDG_STATE_HOME": str(tmp_path / "state"),
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            report_file = Path(result.stdout.strip())
            self.assertTrue(report_file.name.startswith("current."))
            report_file.unlink()

    def test_omo_report_prefers_running_task_without_disabling_blocked_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir)
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            _ = (root / "running.md").write_text(task_frontmatter(), encoding="utf-8")
            _ = (root / "blocked.md").write_text(
                task_frontmatter(status="blocked").replace("status: blocked", "status: blocked\nblocked_on: waiting for human review"),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "OMO_MANAGER_LOCAL_ENV": str(local_env),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMUX_PANE": "%1701",
                "XDG_STATE_HOME": str(tmp_path / "state"),
            }
            cases = (
                ("collision", "current:\nrunning.md cfg:7\nblocked.md cfg:7\n", "running."),
                ("blocked only", "current:\nblocked.md cfg:7\n", "blocked."),
            )
            for name, todo_text, prefix in cases:
                with self.subTest(name=name):
                    (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                    result = subprocess.run(
                        [str(OMO_DIR / "omo_report.sh"), "--alloc-message-file"],
                        cwd=tmp,
                        env=env,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )

                    self.assertEqual(0, result.returncode, result.stderr)
                    report_file = Path(result.stdout.strip())
                    self.assertTrue(report_file.name.startswith(prefix))
                    report_file.unlink()

    def test_omo_report_manager_producer_routes_to_upper_manager_without_manual_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir)
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\nproducer.md cfg:7\nupper.md cfg:6\n", encoding="utf-8")
            producer = root / "producer.md"
            producer.write_text(task_frontmatter(runat="cfg:7", managerat="cfg:6", is_manager=True, status="long_running"), encoding="utf-8")
            upper = root / "upper.md"
            upper.write_text(task_frontmatter(runat="cfg:6", managerat="main:0.0", is_manager=True, status="long_running"), encoding="utf-8")
            env = {
                **os.environ,
                "OMO_MANAGER_LOCAL_ENV": str(local_env),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TMUX_PANE": "%1701",
                "XDG_STATE_HOME": str(tmp_path / "state"),
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
            self.assertEqual(0, alloc.returncode, alloc.stderr)
            report_file = Path(alloc.stdout.strip())
            self.assertTrue(report_file.name.startswith("producer."))
            report_file.write_text("manager report\n", encoding="utf-8")

            submit = subprocess.run(
                [str(OMO_DIR / "omo_report.sh"), "--status", "done", "--message-file", str(report_file)],
                cwd=tmp,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(0, submit.returncode, submit.stderr)
            self.assertNotIn("(pending)", producer.read_text(encoding="utf-8"))
            upper_text = upper.read_text(encoding="utf-8")
            self.assertIn("(pending)", upper_text)
            report_match = re.search(r"^\(from agent cfg:7 (/tmp/[^)]+)\)$", upper_text, flags=re.MULTILINE)
            self.assertIsNotNone(report_match)
            assert report_match is not None
            self.assertIn("task-file=producer.md", Path(report_match.group(1)).read_text(encoding="utf-8"))
            self.assertFalse(dated_manager_file(root).exists())

    def test_omo_report_keeps_pane_collisions_ambiguous_without_one_current_match(self) -> None:
        todo_cases = {
            "no current match": "current:\nother.md other:1\n\nhuman pending:\nfirst.md\nsecond.md\n",
            "multiple current matches": "current:\nfirst.md cfg:7\nsecond.md cfg:7\n",
        }
        for name, todo_text in todo_cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                root = tmp_path / "logs"
                root.mkdir()
                bin_dir = tmp_path / "bin"
                bin_dir.mkdir()
                write_fake_tmux(bin_dir)
                local_env = tmp_path / "local.env"
                local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
                (root / "TODO.md").write_text(todo_text, encoding="utf-8")
                _ = (root / "first.md").write_text(task_frontmatter(), encoding="utf-8")
                _ = (root / "second.md").write_text(task_frontmatter(), encoding="utf-8")
                _ = (root / "other.md").write_text(task_frontmatter(runat="other:1"), encoding="utf-8")

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
                self.assertIn("multiple active task files match tmux target cfg:7", result.stderr)

    def test_omo_report_infers_zero_pane_alias_from_running_previous_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir, session="vl", window="20")
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\n\nprevious:\nvl_ab_dseval_11375.md vl:20\n", encoding="utf-8")
            _ = (root / "vl_ab_dseval_11375.md").write_text(task_frontmatter(runat="vl:20", managerat="vl:2"), encoding="utf-8")

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

            self.assertEqual("", result.stderr)
            self.assertEqual(0, result.returncode)
            report_file = Path(result.stdout.strip())
            self.assertTrue(report_file.name.startswith("vl_ab_dseval_11375."))
            report_file.unlink()

    def test_omo_report_does_not_treat_omitted_pane_as_nonzero_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "logs"
            root.mkdir()
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            write_fake_tmux(bin_dir, pane="1")
            local_env = tmp_path / "local.env"
            local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\n", encoding="utf-8")
            (root / "TODO.md").write_text("current:\ntask.md cfg:7\n", encoding="utf-8")
            _ = (root / "task.md").write_text(task_frontmatter(runat="cfg:7"), encoding="utf-8")

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

    def test_omo_report_manager_route_uses_zero_pane_alias_without_prefix_matching(self) -> None:
        cases = (
            ("zero-pane", "vl:20", "vl:20.0", True),
            ("missing-prefix", "wl:1.0", "wl:18.0", False),
        )
        for name, managerat, manager_runat, routes_to_manager in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                root = tmp_path / "logs"
                root.mkdir()
                bin_dir = tmp_path / "bin"
                bin_dir.mkdir()
                write_fake_tmux(bin_dir)
                local_env = tmp_path / "local.env"
                local_env.write_text(f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n", encoding="utf-8")
                (root / "TODO.md").write_text("current:\nworker.md cfg:7\n\nprevious:\nmanager.md manager\n", encoding="utf-8")
                _ = (root / "worker.md").write_text(task_frontmatter(runat="cfg:7", managerat=managerat), encoding="utf-8")
                manager = root / "manager.md"
                _ = manager.write_text(task_frontmatter(runat=manager_runat, managerat="main:0.0", is_manager=True), encoding="utf-8")
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
                self.assertEqual(0, alloc.returncode, alloc.stderr)
                report_file = Path(alloc.stdout.strip())
                report_file.write_text("routing report\n", encoding="utf-8")

                submit = subprocess.run(
                    [str(OMO_DIR / "omo_report.sh"), "--status", "done", "--message-file", str(report_file)],
                    cwd=tmp,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )

                self.assertEqual(0, submit.returncode, submit.stderr)
                self.assertEqual(routes_to_manager, "(pending)" in manager.read_text(encoding="utf-8"))
                if not routes_to_manager:
                    main_text = dated_manager_file(root).read_text(encoding="utf-8")
                    report_match = re.search(r"^\(from agent cfg:7 (/tmp/[^)]+)\)$", main_text, flags=re.MULTILINE)
                    self.assertIsNotNone(report_match)
                    assert report_match is not None
                    durable_text = Path(report_match.group(1)).read_text(encoding="utf-8")
                    self.assertIn("Target manager `wl:1.0` has no active manager task file", durable_text)

    def test_omo_report_inference_failure_does_not_offer_explicit_task_file_fallback(self) -> None:
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
            self.assertNotIn("pass --task-file explicitly", result.stderr)
            self.assertNotIn("--task-file", result.stderr)

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
            self.assertIn('omo_report.sh --status STATUS --message-file "$REPORT_FILE"', prompt)
            self.assertNotIn("--agent agent-name", prompt)
            self.assertNotIn("--task-file", prompt)
            self.assertNotIn("--root", prompt)
            self.assertIn("editor/file-editing tool", prompt)
            self.assertIn("Do not use cat, heredocs, or shell text injection for report bodies.", prompt)
            self.assertNotIn("Fallback only if task inference fails", prompt)


if __name__ == "__main__":
    unittest.main()
