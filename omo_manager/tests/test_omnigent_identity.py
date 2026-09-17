from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_omnigent_identity import OmniGentIdentityError
from omo_manager.omo_omnigent_identity import authenticate_current_omnigent
from omo_manager.omo_omnigent_identity import authenticate_omnigent_session


class OmniGentIdentityTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path]:
        bridge_root = root / "home" / ".omnigent" / "codex-native"
        bridge = bridge_root / "bridge"
        codex_home = bridge / "codex-home"
        codex_home.mkdir(parents=True)
        state = {
            "active_turn_id": None,
            "codex_home": str(codex_home),
            "cwd": None,
            "session_id": "session-1",
            "socket_path": "ws://127.0.0.1:9876",
            "thread_id": "thread-1",
        }
        state_path = bridge / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)
        proc = root / "proc"
        (proc / "200").mkdir(parents=True)
        (proc / "200" / "cmdline").write_bytes(b"/usr/bin/codex\0app-server\0--listen\0ws://127.0.0.1:9876\0")
        (proc / "200" / "environ").write_bytes(f"CODEX_HOME={codex_home}\0".encode())
        (proc / "200" / "status").write_text("PPid:\t1\n", encoding="utf-8")
        return bridge_root, codex_home

    def session(self) -> dict[str, object]:
        return {
            "archived": False,
            "external_session_id": "thread-1",
            "harness": "codex-native",
            "host_online": True,
            "id": "session-1",
            "runner_online": True,
            "status": "idle",
            "workspace": "/work",
        }

    def test_current_process_ancestry_authenticates_exact_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, codex_home = self.fixture(root)
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_current_omnigent(
                    codex_home=codex_home,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)
            self.assertEqual("/work", identity.workspace)

    def test_current_identity_rejects_spoofed_environment_without_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, codex_home = self.fixture(root)
            (root / "proc" / "300").mkdir()
            (root / "proc" / "300" / "cmdline").write_bytes(b"python\0worker.py\0")
            (root / "proc" / "300" / "environ").write_bytes(f"CODEX_HOME={codex_home}\0".encode())
            (root / "proc" / "300" / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=300),
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()),
                self.assertRaisesRegex(OmniGentIdentityError, "not descended"),
            ):
                authenticate_current_omnigent(codex_home=codex_home, bridge_root=bridge_root, proc_root=root / "proc")

    def test_global_authentication_rejects_duplicate_app_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, codex_home = self.fixture(root)
            duplicate = root / "proc" / "201"
            duplicate.mkdir()
            (duplicate / "cmdline").write_bytes((root / "proc" / "200" / "cmdline").read_bytes())
            (duplicate / "environ").write_bytes((root / "proc" / "200" / "environ").read_bytes())
            (duplicate / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()), self.assertRaisesRegex(OmniGentIdentityError, "found 2"):
                authenticate_omnigent_session(
                    codex_home=codex_home,
                    ancestor_pid=None,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )

    def test_bridge_state_rejects_unsafe_mode_or_hard_link(self) -> None:
        for defect in ("mode", "hard-link"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                bridge_root, codex_home = self.fixture(root)
                state = codex_home.parent / "state.json"
                if defect == "mode":
                    state.chmod(0o644)
                else:
                    (root / "state-link.json").hardlink_to(state)
                with (
                    patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()),
                    self.assertRaisesRegex(OmniGentIdentityError, "safe owner-controlled"),
                ):
                    authenticate_omnigent_session(
                        codex_home=codex_home,
                        ancestor_pid=None,
                        bridge_root=bridge_root,
                        proc_root=root / "proc",
                    )


if __name__ == "__main__":
    unittest.main()
