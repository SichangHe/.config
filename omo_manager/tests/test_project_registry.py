from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from omo_manager.omo_project_registry import parse_sections, validate_registry, validate_summary


SCRIPT = Path.home() / ".config/omo_manager/omo_project_registry.py"


class ProjectRegistryTests(unittest.TestCase):
    def test_upsert_creates_main_owned_registry_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "vl_summary_for_main.md"
            (root / "vl_supervisor_current_7404.md").write_text("runat: vl:9 codex\n", encoding="utf-8")
            summary.write_text(
                "\n".join(
                    [
                        "- project: vl",
                        "- status: active",
                        "- owner: vl:9",
                        "- goal: run VL evaluations",
                        "- state: F prep running",
                        "- next-action: consume F prep",
                        "- blocker: D/C isolated-container auth",
                        "- risk: blocked no-helper launches must not run",
                        "- last-heartbeat: 2026-06-28 12:45 PDT",
                        "- next-checkpoint: 2026-06-28 14:00 PDT",
                        "- evidence: vl_supervisor_current_7404.md",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--root",
                    str(root),
                    "upsert",
                    "--project-id",
                    "vl",
                    "--name",
                    "VeruLaw",
                    "--status",
                    "active",
                    "--submanager-target",
                    "vl:9",
                    "--submanager-task",
                    "vl_supervisor_current_7404.md",
                    "--summary-file",
                    "vl_summary_for_main.md",
                    "--goal",
                    "run VL evaluations",
                    "--blocker",
                    "D/C isolated-container auth",
                    "--last-heartbeat",
                    "2026-06-28 12:45 PDT",
                    "--next-checkpoint",
                    "2026-06-28 14:00 PDT",
                ],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            projects = parse_sections((root / "manager_projects.md").read_text(encoding="utf-8"))
            self.assertEqual("vl:9", projects["vl"]["submanager-target"])
            self.assertEqual([], validate_registry(root / "manager_projects.md", root))

    def test_summary_validation_reports_missing_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            path.write_text("- project: vl\n- status: active\n", encoding="utf-8")
            problems = validate_summary(path, "vl")
            self.assertEqual(1, len(problems))
            self.assertIn("missing summary fields", problems[0])
            self.assertIn("next-action", problems[0])

    def test_registry_check_rejects_owner_mismatch_and_missing_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "summary.md").write_text(
                "\n".join(
                    [
                        "- project: vl",
                        "- status: active",
                        "- owner: vl:10",
                        "- goal: run VL evaluations",
                        "- state: F prep running",
                        "- next-action: consume F prep",
                        "- blocker: D/C isolated-container auth",
                        "- risk: blocked no-helper launches must not run",
                        "- last-heartbeat: 2026-06-28 12:45 PDT",
                        "- next-checkpoint: 2026-06-28 14:00 PDT",
                        "- evidence: vl_supervisor_current_7404.md",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "manager_projects.md").write_text(
                "\n".join(
                    [
                        "# manager project registry",
                        "",
                        "## vl",
                        "- name: VeruLaw",
                        "- status: active",
                        "- submanager-target: vl:9",
                        "- submanager-task: missing.md",
                        "- summary-file: summary.md",
                        "- goal: run VL evaluations",
                        "- blocker: D/C isolated-container auth",
                        "- last-heartbeat: 2026-06-28 12:45 PDT",
                        "- next-checkpoint: 2026-06-28 14:00 PDT",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            problems = validate_registry(root / "manager_projects.md", root)
            self.assertTrue(any("summary owner vl:10" in problem for problem in problems))
            self.assertTrue(any("missing submanager task" in problem for problem in problems))

    def test_registry_check_rejects_out_of_root_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root.parent / "outside_summary.md"
            outside.write_text("- project: vl\n", encoding="utf-8")
            (root / "vl_supervisor_current_7404.md").write_text("runat: vl:9 codex\n", encoding="utf-8")
            (root / "manager_projects.md").write_text(
                "\n".join(
                    [
                        "# manager project registry",
                        "",
                        "## vl",
                        "- name: VeruLaw",
                        "- status: active",
                        "- submanager-target: vl:9",
                        "- submanager-task: vl_supervisor_current_7404.md",
                        f"- summary-file: {outside}",
                        "- goal: run VL evaluations",
                        "- blocker: D/C isolated-container auth",
                        "- last-heartbeat: 2026-06-28 12:45 PDT",
                        "- next-checkpoint: 2026-06-28 14:00 PDT",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                validate_registry(root / "manager_projects.md", root)

    def test_single_summary_cli_check_accepts_any_project_without_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = root / "vl_summary_for_main.md"
            summary.write_text(
                "\n".join(
                    [
                        "- project: vl",
                        "- status: active",
                        "- owner: vl:9",
                        "- goal: run VL evaluations",
                        "- state: F prep running",
                        "- next-action: consume F prep",
                        "- blocker: D/C isolated-container auth",
                        "- risk: blocked no-helper launches must not run",
                        "- last-heartbeat: 2026-06-28 12:45 PDT",
                        "- next-checkpoint: 2026-06-28 14:00 PDT",
                        "- evidence: vl_supervisor_current_7404.md",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [str(SCRIPT), "--root", str(root), "check", "--summary-file", "vl_summary_for_main.md"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)

    def test_cli_rejects_out_of_root_registry_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root.parent / "outside_registry.md"
            result = subprocess.run(
                [str(SCRIPT), "--root", str(root), "--registry", str(outside), "init"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("path must stay under root", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
