"""Exercise installed bridges against real tmux TUIs that never echo input."""

import importlib
import importlib.metadata
import importlib.util
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "amh"))
HAS_OMNIGENT = importlib.util.find_spec("omnigent") is not None


@unittest.skipUnless(HAS_OMNIGENT and shutil.which("tmux"), "requires installed OmniGent and tmux")
class NativeNoRenderDeliveryTests(unittest.TestCase):
    def test_all_paste_bridges_submit_once_without_echo(self) -> None:
        print(f"installed omnigent_version={importlib.metadata.version('omnigent')} python={sys.executable}", flush=True)
        for harness in (
            "antigravity_native", "claude_native", "cursor_native", "goose_native",
            "hermes_native", "kimi_native", "kiro_native", "devin_native", "amh",
        ):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory(prefix="no-render-tui-") as directory:
                root = Path(directory)
                socket = str(root / "tmux.sock")
                received_file = root / "received.bin"
                ready_file = root / "ready"
                db_file = root / "state.db"
                program = (
                    "import os,sys,tty,sqlite3; from pathlib import Path; tty.setraw(0); "
                    "db=sqlite3.connect(sys.argv[3]); db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY)'); db.commit(); "
                    "os.write(1,sys.argv[4].encode()); "
                    "os.write(1,b'\\x1b[?2004h'); Path(sys.argv[2]).touch(); "
                    "output=open(sys.argv[1],'ab',buffering=0); received=b''; recorded=False\n"
                    "while True:\n"
                    " chunk=os.read(0,65536); output.write(chunk); received+=chunk\n"
                    " if not recorded and b'\\x1b[201~\\r' in received:\n"
                    "  db.execute('INSERT INTO messages DEFAULT VALUES'); db.commit(); recorded=True\n"
                )
                chrome = "1 queued · Enter send now\r\n" if harness == "devin_native" else ""
                subprocess.run(
                    ["tmux", "-S", socket, "-f", "/dev/null", "new-session", "-d", "-s", "main",
                     shlex.join([sys.executable, "-u", "-c", program, str(received_file), str(ready_file), str(db_file), chrome])],
                    check=True, timeout=10,
                )
                try:
                    deadline_s = time.monotonic() + 5
                    while not ready_file.exists() and time.monotonic() < deadline_s:
                        time.sleep(0.01)
                    self.assertTrue(ready_file.exists())
                    bridge = importlib.import_module(
                        "amh.agents" if harness == "amh" else f"omnigent.harnesses.{harness}.bridge"
                    )
                    print(f"imported harness={harness} path={bridge.__file__}", flush=True)
                    info = {"socket_path": socket, "tmux_target": "main"}
                    content = "invisible draft must be submitted\nsecond line"
                    with ExitStack() as guards:
                        for function in (
                            "_settle_pane", "_wait_for_kiro_input_ready", "_wait_for_devin_input_ready",
                            "_wait_for_forwarder_ready_if_required", "_clear_composer",
                            "_wait_for_agy_prompt_ready", "_wait_for_claude_prompt_ready", "_restore_occupied_input",
                        ):
                            if hasattr(bridge, function):
                                guards.enter_context(patch.object(bridge, function))
                        for function in ("_draft_in_input_region", "_draft_in_input_box", "_draft_visible_in_editor"):
                            if hasattr(bridge, function):
                                guards.enter_context(patch.object(bridge, function, side_effect=AssertionError("draft render gate called")))
                        if hasattr(bridge, "_wait_for_tmux_info"):
                            guards.enter_context(patch.object(bridge, "_wait_for_tmux_info", return_value=info))
                        if harness == "amh":
                            def private_tmux(*arguments: str, text_in: str | None = None) -> subprocess.CompletedProcess[str]:
                                return subprocess.run(
                                    ["tmux", "-S", socket, *arguments], input=text_in,
                                    capture_output=True, text=True, check=False, timeout=5,
                                )

                            guards.enter_context(patch.object(bridge, "tmux", side_effect=private_tmux))
                            bridge.send(None, "main", content)
                        elif harness == "antigravity_native":
                            guards.enter_context(patch.object(bridge, "_VERIFY_PROBE_TIMEOUT_S", 0))
                            bridge.inject_user_message_via_tui(root, content=content, timeout_s=0)
                        elif harness == "claude_native":
                            bridge.inject_user_message(root, content=content, timeout_s=0)
                        elif harness == "hermes_native":
                            guards.enter_context(patch.object(bridge, "_state_db_path", return_value=db_file))
                            bridge.inject_user_message(root, content=content, timeout_s=0)
                        else:
                            if harness == "kimi_native":
                                state = SimpleNamespace(
                                    editor_present=True, editor_content="", exit_armed=False,
                                    trust_visible=False, turn_streaming=False,
                                )
                                guards.enter_context(patch.object(bridge, "_parse_pane", return_value=state))
                                guards.enter_context(patch.object(bridge, "_approval_pending", return_value=False))
                            bridge.inject_user_message(root, content=content, timeout_s=0)
                    deadline_s = time.monotonic() + 2
                    received = b""
                    while time.monotonic() < deadline_s:
                        received = received_file.read_bytes()
                        if b"\x1b[201~\r" in received:
                            break
                        time.sleep(0.01)
                    self.assertIn(b"invisible draft must be submitted", received)
                    self.assertEqual(1, received.count(b"\x1b[201~\r"), received)
                    self.assertEqual(b"\r", received.partition(b"\x1b[201~")[2], received)
                    pane = subprocess.run(
                        ["tmux", "-S", socket, "capture-pane", "-p", "-t", "main"],
                        capture_output=True, text=True, check=True, timeout=5,
                    ).stdout
                    self.assertNotIn("invisible draft", pane)
                    if harness == "devin_native":
                        self.assertTrue(bridge.devin_queue_pending(pane))
                finally:
                    subprocess.run(["tmux", "-S", socket, "kill-server"], timeout=5, check=False)
