from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "omo_manager_restart.sh"


class ManagerRestartTests(unittest.TestCase):
    def test_human_owned_target_is_rejected_before_tmux_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "tmux.calls"
            tmux = fake_bin / "tmux"
            tmux.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {calls}\nexit 99\n", encoding="utf-8")
            tmux.chmod(tmux.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(
                [str(SCRIPT), "--tmux-target", "hcfg:1.0", "--state-dir", str(root / "state"), "--dry-run"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "OMO_MANAGER_LOCAL_ENV": str(root / "missing.env")},
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("refuses human-owned", result.stderr)
            self.assertFalse(calls.exists())

    def test_target_rebind_is_rejected_before_any_send_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "tmux.calls"
            tmux = fake_bin / "tmux"
            tmux.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {calls}\n"
                "case \"$1\" in\n"
                "  has-session) exit 0 ;;\n"
                "  display-message)\n"
                f"    count=$(grep -c '^display-message' {calls})\n"
                "    if [ \"$count\" -eq 1 ]; then printf '%s\\n' '$1 %7 mgr:1.0'; else printf '%s\\n' '$2 %8'; fi\n"
                "    exit 0 ;;\n"
                "  send-keys) exit 99 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            tmux.chmod(tmux.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(
                [str(SCRIPT), "--tmux-target", "mgr:1.0", "--root", str(root), "--workdir", str(root), "--state-dir", str(root / "state"), "--no-refresh-watchers", "--no-startup-prompt"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "OMO_MANAGER_LOCAL_ENV": str(root / "missing.env")},
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("changed after identity binding", result.stderr)
            self.assertNotIn("send-keys", calls.read_text(encoding="utf-8"))

    def test_missing_session_is_rejected_without_creating_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            tmux.chmod(tmux.stat().st_mode | stat.S_IXUSR)
            state_dir = root / "state"
            result = subprocess.run(
                [str(SCRIPT), "--tmux-target", "missing:0.0", "--state-dir", str(state_dir), "--dry-run"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("reuse an existing non-human session", result.stderr)
            self.assertFalse(state_dir.exists())

    def test_explicit_missing_session_creation_has_reuse_guidance_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            tmux = fake_bin / "tmux"
            tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            tmux.chmod(tmux.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(
                [
                    str(SCRIPT),
                    "--tmux-target",
                    "needed:0.0",
                    "--workdir",
                    str(root),
                    "--state-dir",
                    str(root / "state"),
                    "--allow-new-tmux-session",
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "OMO_MANAGER_LOCAL_ENV": str(root / "missing.env")},
            )
            self.assertEqual(0, result.returncode)
            self.assertIn("would explicitly create tmux session needed", result.stdout)
            self.assertIn("reuse an existing non-human session", result.stdout)
            self.assertFalse((root / "state").exists())

    def test_target_is_required_without_default_session_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [str(SCRIPT), "--state-dir", str(Path(tmp) / "state"), "--dry-run"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env={
                    **{key: value for key, value in os.environ.items() if key != "OMO_MANAGER_TMUX_TARGET"},
                    "OMO_MANAGER_LOCAL_ENV": str(Path(tmp) / "missing.env"),
                },
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("OMO_MANAGER_TMUX_TARGET or --tmux-target is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
