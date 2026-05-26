import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = Path.home() / ".config/omo_manager/opencode_auth_switch.py"


class OpenCodeAuthSwitchTests(unittest.TestCase):
    def run_helper(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(HELPER), "--auth-dir", str(root), *args],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def write_auth(self, root: Path, name: str, marker: str) -> Path:
        path = root / name
        _ = path.write_text(json.dumps({"marker": marker}), encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_dry_run_does_not_mutate_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            live = self.write_auth(root, "auth.json", "live")
            _ = self.write_auth(root, "auth.current.json", "live")
            _ = self.write_auth(root, "auth.candidate.json", "candidate")
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("switch_status: planned_noop", result.stdout)
            self.assertEqual({"marker": "live"}, json.loads(live.read_text(encoding="utf-8")))

    def test_execute_requires_human_authorized_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            live = self.write_auth(root, "auth.json", "live")
            _ = self.write_auth(root, "auth.current.json", "live")
            _ = self.write_auth(root, "auth.candidate.json", "candidate")
            result = self.run_helper(root, "--current-name", "current", "--candidate-name", "candidate", "--execute")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("blocked_without_human_authorized_switch", result.stdout)
            self.assertEqual({"marker": "live"}, json.loads(live.read_text(encoding="utf-8")))

    def test_execute_switches_and_writes_private_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            backup_root = root / "backups"
            live = self.write_auth(root, "auth.json", "live")
            current = self.write_auth(root, "auth.current.json", "live")
            _ = self.write_auth(root, "auth.candidate.json", "candidate")
            result = self.run_helper(
                root,
                "--current-name",
                "current",
                "--candidate-name",
                "candidate",
                "--backup-root",
                str(backup_root),
                "--execute",
                "--human-authorized-switch",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("switch_status: switched", result.stdout)
            self.assertEqual({"marker": "candidate"}, json.loads(live.read_text(encoding="utf-8")))
            self.assertEqual({"marker": "live"}, json.loads(current.read_text(encoding="utf-8")))
            backups = list(backup_root.glob("*/auth.pre-switch.json"))
            self.assertEqual(1, len(backups))
            self.assertEqual(0o600, backups[0].stat().st_mode & 0o777)
            self.assertEqual({"marker": "live"}, json.loads(backups[0].read_text(encoding="utf-8")))


if __name__ == "__main__":
    _ = unittest.main()
