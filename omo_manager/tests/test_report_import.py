from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


OMO_DIR = Path(__file__).resolve().parents[1]
REPORT = OMO_DIR / "omo_report.sh"
RECEIPT = OMO_DIR / "omo_report_receipt.py"


def task_frontmatter() -> str:
    return """---
version: v1.0.0
status: running
runat: cfg:7
tool: codex
managerat: main:0.0
is_manager: false
pending_task_items: []
---
"""


def report_case(tmp: Path) -> tuple[Path, Path, Path, dict[str, str]]:
    root = tmp / "logs"
    root.mkdir()
    home = tmp / "home"
    home.mkdir()
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text("#!/usr/bin/env bash\nprintf 'cfg\\t7\\t0\\t%%1701\\tworker\\n'\n", encoding="utf-8")
    tmux.chmod(0o700)
    task = root / "worker.md"
    todo = root / "TODO.md"
    message = tmp / "report.md"
    task.write_text(task_frontmatter(), encoding="utf-8")
    todo.write_text("current:\nworker.md cfg:7\n", encoding="utf-8")
    message.write_text("private report\n", encoding="utf-8")
    message.chmod(0o600)
    local_env = tmp / "local.env"
    local_env.write_text(
        f"OMO_WORK_LOGS_ROOT={root}\nOMO_MANAGER_TMUX_TARGET=main:0.0\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "CODEX_HOME": str(tmp / "codex-home"),
        "HOME": str(home),
        "OMO_MANAGER_LOCAL_ENV": str(local_env),
        "OMO_MANAGER_TMUX_TARGET": "main:0.0",
        "OMO_WORK_LOGS_ROOT": str(root),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMUX_PANE": "%1701",
        "XDG_STATE_HOME": str(tmp / "state"),
    }
    return task, todo, message, env


def describe_command(report: Path, message: Path) -> list[str]:
    return [
        str(report),
        "--describe",
        "--status",
        "in-progress",
        "--message-file",
        str(message),
        "--agent",
        "report-import-test",
    ]


class ReportImportTests(unittest.TestCase):
    def test_direct_receiver_refuses_unauthenticated_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-I", str(RECEIPT), "--help"],
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(2, result.returncode)
        self.assertEqual("omo_report_receipt.py must be loaded through the adjacent omo_report.sh\n", result.stderr)
        self.assertEqual("", result.stdout)

    def test_package_import_uses_report_dependencies(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "omo_manager.omo_report_receipt", "--help"],
            cwd=OMO_DIR.parent,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Commit one authenticated agent report", result.stdout)

    def test_wrapper_loads_neighboring_identity_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            _task, _todo, message, env = report_case(tmp)
            package = tmp / "copied" / "omo_manager"
            package.mkdir(parents=True)
            for name in (
                "omo_omnigent_identity.py",
                "omo_pending_digest.py",
                "omo_report.sh",
                "omo_report_receipt.py",
                "omo_task_lock.py",
            ):
                shutil.copy2(OMO_DIR / name, package / name)
            report = package / "omo_report.sh"
            report.chmod(0o700)
            (package / "omo_omnigent_identity.py").write_text(
                "raise RuntimeError('neighbor identity sentinel')\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                describe_command(report, message),
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("neighbor identity sentinel", result.stderr)

    # 🧑 "omo_report.sh fails before routing because ... omo_report_receipt.py cannot import .omo_omnigent_identity"
    def test_report_wrapper_describes_with_exact_local_identity_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            task, todo, message, env = report_case(tmp)
            before = (task.read_bytes(), todo.read_bytes(), message.read_bytes())

            result = subprocess.run(
                describe_command(REPORT, message),
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            description = json.loads(result.stdout)
            self.assertEqual("omo-report-description/v1", description["schema"])
            self.assertEqual("omo-report-preflight-binding/v1", description["transaction"]["schema"])
            self.assertEqual(before, (task.read_bytes(), todo.read_bytes(), message.read_bytes()))
            self.assertFalse(Path(description["files"]["manager"]).exists())
            self.assertFalse(Path(description["files"]["private_envelope"]).exists())
            self.assertFalse(Path(description["files"]["private_receipt"]).exists())
            self.assertFalse(Path(description["files"]["receipt_publication"]).exists())
            self.assertTrue(all(not Path(path).exists() for path in description["temporary_files"]))
            self.assertFalse((tmp / "state").exists())


if __name__ == "__main__":
    unittest.main()
