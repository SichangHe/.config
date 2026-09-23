from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_omnigent_identity import OmniGentIdentityError
from omo_manager.omo_omnigent_identity import authenticate_antigravity_session
from omo_manager.omo_omnigent_identity import authenticate_current_omnigent
from omo_manager.omo_omnigent_identity import authenticate_cursor_session
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


class AntigravityIdentityTests(unittest.TestCase):
    def fixture(self, root: Path, *, conversation_id: str = "thread-agy-1") -> tuple[Path, Path]:
        bridge_root = root / "home" / ".omnigent" / "antigravity-native"
        bridge = bridge_root / "bridge"
        gemini = bridge / "agy-home" / ".gemini"
        gemini.mkdir(parents=True)
        state = {
            "active_turn_id": None,
            "conversation_id": conversation_id,
            "session_id": "session-agy-1",
        }
        state_path = bridge / "state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        state_path.chmod(0o600)
        proc = root / "proc"
        (proc / "200").mkdir(parents=True)
        (proc / "200" / "cmdline").write_bytes(f"/usr/bin/agy\0--gemini_dir={gemini}\0".encode())
        (proc / "200" / "environ").write_bytes(f"HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR={bridge}\0HARNESS_ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID=session-agy-1\0".encode())
        (proc / "200" / "status").write_text("PPid:\t1\n", encoding="utf-8")
        return bridge_root, bridge

    def runner_environ(self) -> bytes:
        return ("OMNIGENT_RUNNER_LAUNCH_HARNESS=antigravity-native\0OMNIGENT_RUNNER_PRIMARY_SESSION_ID=session-agy-1\0").encode()

    def write_conversation(self, bridge: Path, conversation_id: str) -> None:
        path = bridge / "agy-home" / ".gemini" / "antigravity-cli" / "conversations" / f"{conversation_id}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"agy")
        path.chmod(0o600)

    def session(self, *, external_session_id: str | None = "thread-agy-1") -> dict[str, object]:
        return {
            "archived": False,
            "external_session_id": external_session_id,
            "harness": "antigravity-native",
            "host_online": True,
            "id": "session-agy-1",
            "runner_online": True,
            "status": "idle",
            "workspace": "/work",
        }

    def test_current_process_ancestry_authenticates_exact_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_current_omnigent(
                    antigravity_bridge=bridge,
                    antigravity_bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-agy-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)
            self.assertEqual("/work", identity.workspace)
            self.assertEqual(str(bridge / "agy-home"), identity.codex_home)
            self.assertEqual(str((bridge / "agy-home" / ".gemini").resolve()), identity.socket_path)

    def test_current_process_discovers_bridge_from_agy_gemini_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (root / "proc" / "200" / "environ").write_bytes(self.runner_environ())
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_current_omnigent(
                    antigravity_bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-agy-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)

    def test_runner_shell_authenticates_without_agy_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (root / "proc" / "200" / "environ").write_bytes(self.runner_environ())
            helper = root / "proc" / "300"
            helper.mkdir()
            (helper / "cmdline").write_bytes(b"python\0sys_os_shell\0")
            (helper / "environ").write_bytes(self.runner_environ())
            (helper / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=300), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_current_omnigent(
                    antigravity_bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-agy-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)

    def test_current_identity_rejects_spoofed_environment_without_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (root / "proc" / "300").mkdir()
            (root / "proc" / "300" / "cmdline").write_bytes(b"python\0worker.py\0")
            (root / "proc" / "300" / "environ").write_bytes(f"HARNESS_ANTIGRAVITY_NATIVE_BRIDGE_DIR={bridge}\0HARNESS_ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID=session-agy-1\0".encode())
            (root / "proc" / "300" / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=300),
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()),
                self.assertRaisesRegex(OmniGentIdentityError, "not descended"),
            ):
                authenticate_current_omnigent(antigravity_bridge=bridge, antigravity_bridge_root=bridge_root, proc_root=root / "proc")

    def test_placeholder_conversation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root, conversation_id="agy_conv_pending")
            with (
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()),
                self.assertRaisesRegex(OmniGentIdentityError, "not ready"),
            ):
                authenticate_antigravity_session(
                    bridge_dir=bridge,
                    ancestor_pid=200,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )

    def test_placeholder_uses_unique_on_disk_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root, conversation_id="agy_conv_pending")
            conversation_id = "3a976813-79e8-442b-b7fc-d7b753d80887"
            self.write_conversation(bridge, conversation_id)
            session = self.session(external_session_id="agy_conv_pending")
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=session):
                identity = authenticate_antigravity_session(
                    bridge_dir=bridge,
                    ancestor_pid=200,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual(conversation_id, identity.thread_id)

    def test_real_conversation_allows_unset_external_when_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conversation_id = "3a976813-79e8-442b-b7fc-d7b753d80887"
            bridge_root, bridge = self.fixture(root, conversation_id=conversation_id)
            self.write_conversation(bridge, conversation_id)
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session(external_session_id=None)):
                identity = authenticate_antigravity_session(
                    bridge_dir=bridge,
                    ancestor_pid=200,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual(conversation_id, identity.thread_id)

    def test_placeholder_rejects_duplicate_on_disk_conversations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root, conversation_id="agy_conv_pending")
            self.write_conversation(bridge, "3a976813-79e8-442b-b7fc-d7b753d80887")
            extra = bridge / "agy-home" / ".gemini" / "antigravity-cli" / "conversations" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.db"
            extra.write_bytes(b"agy")
            extra.chmod(0o600)
            with (
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session(external_session_id=None)),
                self.assertRaisesRegex(OmniGentIdentityError, "expected one Antigravity conversation"),
            ):
                authenticate_antigravity_session(
                    bridge_dir=bridge,
                    ancestor_pid=200,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )

    def test_global_authentication_rejects_duplicate_antigravity_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            duplicate = root / "proc" / "201"
            duplicate.mkdir()
            (duplicate / "cmdline").write_bytes((root / "proc" / "200" / "cmdline").read_bytes())
            (duplicate / "environ").write_bytes((root / "proc" / "200" / "environ").read_bytes())
            (duplicate / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()), self.assertRaisesRegex(OmniGentIdentityError, "found 2"):
                authenticate_antigravity_session(
                    bridge_dir=bridge,
                    ancestor_pid=None,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )


class CursorIdentityTests(unittest.TestCase):
    def digest(self, session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()[:32]

    def fixture(self, root: Path, *, session_id: str = "session-cursor-1", thread_id: str = "3a976813-79e8-442b-b7fc-d7b753d80887") -> tuple[Path, Path]:
        bridge_root = root / "tmp" / "omnigent-1" / "cursor-native"
        bridge = bridge_root / self.digest(session_id)
        bridge.mkdir(parents=True)
        tmux_path = bridge / "tmux.json"
        tmux_path.write_text(json.dumps({"socket_path": "/tmp/cursor.sock", "tmux_target": "main"}), encoding="utf-8")
        tmux_path.chmod(0o644)
        forwarder = bridge / "cursor_forwarder.json"
        forwarder.write_text(json.dumps({"store_path": f"/cursor/chats/{thread_id}/store.db", "last_rowid": 1}), encoding="utf-8")
        forwarder.chmod(0o644)
        proc = root / "proc"
        (proc / "200").mkdir(parents=True)
        (proc / "200" / "cmdline").write_bytes(b"/usr/bin/agent\0--force\0--trust\0")
        (proc / "200" / "environ").write_bytes(f"HARNESS_CURSOR_NATIVE_BRIDGE_DIR={bridge}\0OMNIGENT_RUNNER_LAUNCH_HARNESS=cursor-native\0OMNIGENT_RUNNER_PRIMARY_SESSION_ID={session_id}\0".encode())
        (proc / "200" / "status").write_text("PPid:\t1\n", encoding="utf-8")
        return bridge_root, bridge

    def runner_environ(self, bridge: Path, session_id: str = "session-cursor-1") -> bytes:
        return (f"OMNIGENT_RUNNER_LAUNCH_HARNESS=cursor-native\0OMNIGENT_RUNNER_PRIMARY_SESSION_ID={session_id}\0").encode()

    def session(self, *, external_session_id: str | None = "3a976813-79e8-442b-b7fc-d7b753d80887") -> dict[str, object]:
        return {
            "archived": False,
            "external_session_id": external_session_id,
            "harness": "cursor-native",
            "host_online": True,
            "id": "session-cursor-1",
            "runner_online": True,
            "status": "idle",
            "workspace": "/work",
        }

    def test_current_process_ancestry_authenticates_exact_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_current_omnigent(
                    cursor_bridge=bridge,
                    cursor_bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-cursor-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)
            self.assertEqual("/work", identity.workspace)
            self.assertEqual(str(bridge.resolve()), identity.codex_home)
            self.assertEqual("/tmp/cursor.sock", identity.socket_path)
            self.assertEqual(str(bridge / "tmux.json"), identity.state_path)

    def test_runner_shell_authenticates_without_agent_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            helper = root / "proc" / "300"
            helper.mkdir()
            (helper / "cmdline").write_bytes(b"python\0sys_os_shell\0")
            (helper / "environ").write_bytes(self.runner_environ(bridge))
            (helper / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=300), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_current_omnigent(
                    cursor_bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-cursor-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)

    def test_current_env_runner_session_without_ancestor_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            helper = root / "proc" / "300"
            helper.mkdir()
            (helper / "cmdline").write_bytes(b"python\0sys_os_shell\0")
            (helper / "environ").write_bytes(b"PATH=/usr/bin\0")
            (helper / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=300),
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()),
                patch.dict(
                    os.environ,
                    {
                        "OMNIGENT_RUNNER_LAUNCH_HARNESS": "cursor-native",
                        "OMNIGENT_RUNNER_PRIMARY_SESSION_ID": "session-cursor-1",
                    },
                    clear=False,
                ),
            ):
                identity = authenticate_current_omnigent(
                    cursor_bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("omnigent://session-cursor-1", identity.target)
            self.assertEqual(200, identity.app_server_pid)

    def test_current_identity_rejects_spoofed_environment_without_ancestry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (root / "proc" / "300").mkdir()
            (root / "proc" / "300" / "cmdline").write_bytes(b"python\0worker.py\0")
            (root / "proc" / "300" / "environ").write_bytes(
                f"HARNESS_CURSOR_NATIVE_BRIDGE_DIR={bridge}\0OMNIGENT_RUNNER_LAUNCH_HARNESS=cursor-native\0OMNIGENT_RUNNER_PRIMARY_SESSION_ID=session-cursor-1\0".encode()
            )
            (root / "proc" / "300" / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with (
                patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=300),
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()),
                self.assertRaisesRegex(OmniGentIdentityError, "not descended"),
            ):
                authenticate_current_omnigent(cursor_bridge=bridge, cursor_bridge_root=bridge_root, proc_root=root / "proc")

    def test_missing_chat_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (bridge / "cursor_forwarder.json").unlink()
            with (
                patch("omo_manager.omo_omnigent_identity._session", return_value=self.session(external_session_id=None)),
                self.assertRaisesRegex(OmniGentIdentityError, "not ready"),
            ):
                authenticate_cursor_session(
                    bridge_dir=bridge,
                    session_id="session-cursor-1",
                    ancestor_pid=200,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )

    def test_forwarder_chat_allows_unset_external(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            with patch("omo_manager.omo_omnigent_identity.os.getppid", return_value=200), patch("omo_manager.omo_omnigent_identity._session", return_value=self.session(external_session_id=None)):
                identity = authenticate_cursor_session(
                    bridge_dir=bridge,
                    session_id="session-cursor-1",
                    ancestor_pid=200,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual("3a976813-79e8-442b-b7fc-d7b753d80887", identity.thread_id)

    def test_workspace_argv_binds_without_bridge_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (root / "proc" / "200" / "cmdline").write_bytes(b"/usr/bin/agent\0--force\0--workspace\0/work\0")
            (root / "proc" / "200" / "environ").write_bytes(b"PATH=/usr/bin\0")
            with patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_cursor_session(
                    bridge_dir=bridge,
                    session_id="session-cursor-1",
                    ancestor_pid=None,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual(200, identity.app_server_pid)

    def test_models_helper_is_not_the_bound_tui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            (root / "proc" / "200" / "cmdline").write_bytes(b"/usr/bin/cursor-agent\0--force\0--trust\0--workspace\0/work\0")
            (root / "proc" / "200" / "environ").write_bytes(b"PATH=/usr/bin\0")
            helper = root / "proc" / "201"
            helper.mkdir()
            (helper / "cmdline").write_bytes(b"/usr/bin/cursor-agent\0models\0")
            (helper / "environ").write_bytes(b"PATH=/usr/bin\0")
            (helper / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()):
                identity = authenticate_cursor_session(
                    bridge_dir=bridge,
                    session_id="session-cursor-1",
                    ancestor_pid=None,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )
            self.assertEqual(200, identity.app_server_pid)

    def test_global_authentication_rejects_duplicate_cursor_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge_root, bridge = self.fixture(root)
            duplicate = root / "proc" / "201"
            duplicate.mkdir()
            (duplicate / "cmdline").write_bytes((root / "proc" / "200" / "cmdline").read_bytes())
            (duplicate / "environ").write_bytes((root / "proc" / "200" / "environ").read_bytes())
            (duplicate / "status").write_text("PPid:\t1\n", encoding="utf-8")
            with patch("omo_manager.omo_omnigent_identity._session", return_value=self.session()), self.assertRaisesRegex(OmniGentIdentityError, "found 2"):
                authenticate_cursor_session(
                    bridge_dir=bridge,
                    session_id="session-cursor-1",
                    ancestor_pid=None,
                    bridge_root=bridge_root,
                    proc_root=root / "proc",
                )


if __name__ == "__main__":
    unittest.main()
