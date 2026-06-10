#!/usr/bin/env python3
"""Run account-rotation validation with pass/fail-only output."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path.home() / ".config"
WORK_LOGS = Path("/ssd1/sichangheagent/work_logs")


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path


def checks() -> list[Check]:
    return [
        Check(
            "py_compile",
            (
                sys.executable,
                "-m",
                "py_compile",
                str(ROOT / "omo_manager/opencode_auth_rotation_dryrun.py"),
                str(ROOT / "omo_manager/opencode_rotation_quiet_check.py"),
                str(ROOT / "omo_manager/opencode_quota_profile_watch.py"),
                str(ROOT / "omo_manager/tests/test_opencode_auth_rotation_dryrun.py"),
                str(ROOT / "omo_manager/tests/test_opencode_quota_profile_watch.py"),
            ),
            ROOT,
        ),
        Check(
            "ruff",
            (
                "ruff",
                "check",
                str(ROOT / "omo_manager/opencode_auth_rotation_dryrun.py"),
                str(ROOT / "omo_manager/opencode_rotation_quiet_check.py"),
                str(ROOT / "omo_manager/opencode_quota_profile_watch.py"),
                str(ROOT / "omo_manager/tests/test_opencode_auth_rotation_dryrun.py"),
                str(ROOT / "omo_manager/tests/test_opencode_quota_profile_watch.py"),
            ),
            ROOT,
        ),
        Check(
            "helper_tests",
            (sys.executable, "-m", "unittest", "omo_manager.tests.test_opencode_auth_rotation_dryrun"),
            ROOT,
        ),
        Check(
            "quota_watcher_tests",
            (sys.executable, "-m", "unittest", "omo_manager.tests.test_opencode_quota_profile_watch"),
            ROOT,
        ),
        Check("cfg_tests", (sys.executable, "-m", "unittest", "discover", "-s", "omo_manager/tests"), ROOT),
        Check(
            "work_logs_tests",
            (sys.executable, "-m", "unittest", "discover", "-s", str(WORK_LOGS / "tests")),
            WORK_LOGS,
        ),
        Check(
            "cfg_diff_check",
            (
                "git",
                "diff",
                "--check",
                "--",
                "omo_manager/opencode_auth_rotation_dryrun.py",
                "omo_manager/opencode_rotation_quiet_check.py",
                "omo_manager/opencode_quota_profile_watch.py",
                "omo_manager/tests/test_opencode_auth_rotation_dryrun.py",
                "omo_manager/tests/test_opencode_quota_profile_watch.py",
            ),
            ROOT,
        ),
        Check(
            "work_logs_diff_check",
            (
                "git",
                "diff",
                "--check",
                "--",
                "manager_opencode_account_rotation_runbook.md",
                "manager_opencode_account_rotation_4104.md",
                "work_manager.md",
                "tests/test_omo_manager.py",
            ),
            WORK_LOGS,
        ),
    ]


def main() -> int:
    failures: list[str] = []
    for check in checks():
        try:
            result = subprocess.run(
                check.command,
                cwd=check.cwd,
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            failures.append(check.name)
            continue
        if result.returncode != 0:
            failures.append(check.name)
    if failures:
        print("aggregate_validation: FAIL")
        print("failed_commands: " + ", ".join(failures))
        return 1
    print("aggregate_validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
