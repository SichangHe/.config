from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import call, patch

from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import resume_cmd
from omo_manager.omo_omnigent import (
    SessionSnapshot,
    antigravity_terminal_ready,
    bind_local_antigravity_conversation,
    launch_session,
    manager_status,
    online_host_id,
    send_message,
    session_ready_report,
    session_snapshot,
    stop_session,
)


class OmniGentRuntimeTests(unittest.TestCase):
    @patch("omo_manager.omo_omnigent.antigravity_terminal_capture")
    def test_antigravity_ready_requires_the_latest_delivered_prompt(self, capture) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root / "bridge"
            bridge.mkdir()
            state = bridge / "state.json"
            state.write_text('{"conversation_id":"agy_conv_pending","session_id":"session-agy"}', encoding="utf-8")
            state.chmod(0o600)
            old_capture = "─" * 20 + "\n> old prompt\nold answer\n" + "─" * 20 + "\n>\n" + "─" * 20 + "\n? for shortcuts"
            marker = bridge / "omo-manager-expected-turn.json"
            marker.write_text(
                json.dumps(
                    {
                        "delivery_id": "delivery-1",
                        "message_suffix": "latest prompt",
                        "prior_capture_sha256": hashlib.sha256(old_capture.encode()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            capture.return_value = old_capture
            with patch.dict("os.environ", {"OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT": str(root)}):
                self.assertFalse(antigravity_terminal_ready("omnigent://session-agy"))
                capture.return_value = "─" * 20 + "\n> latest prompt\nnew answer\n" + "─" * 20 + "\n>\n" + "─" * 20 + "\n? for shortcuts"
                self.assertTrue(antigravity_terminal_ready("omnigent://session-agy"))

    @patch("omo_manager.omo_omnigent.antigravity_terminal_capture")
    def test_antigravity_identical_retry_waits_for_a_new_prompt_occurrence(self, capture) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root / "bridge"
            bridge.mkdir()
            state = bridge / "state.json"
            state.write_text('{"conversation_id":"conversation","session_id":"session-agy"}', encoding="utf-8")
            state.chmod(0o600)
            turn = "─" * 20 + "\n> repeat prompt\nanswer\n" + "─" * 20 + "\n>\n" + "─" * 20 + "\n? for shortcuts"
            marker = bridge / "omo-manager-expected-turn.json"
            marker.write_text(
                json.dumps(
                    {
                        "delivery_id": "delivery-2",
                        "message_suffix": "repeat prompt",
                        "prior_capture_sha256": hashlib.sha256(turn.encode()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            capture.return_value = turn
            with patch.dict("os.environ", {"OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT": str(root)}):
                self.assertFalse(antigravity_terminal_ready("omnigent://session-agy"))
                capture.return_value = turn + "\n" + turn
                self.assertTrue(antigravity_terminal_ready("omnigent://session-agy"))

    @patch("omo_manager.omo_omnigent.antigravity_terminal_capture")
    def test_antigravity_running_tool_is_not_ready(self, capture) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root / "bridge"
            bridge.mkdir()
            state = bridge / "state.json"
            state.write_text('{"conversation_id":"conversation","session_id":"session-agy"}', encoding="utf-8")
            state.chmod(0o600)
            prior = "─" * 20 + "\n>\n" + "─" * 20 + "\n? for shortcuts"
            marker = bridge / "omo-manager-expected-turn.json"
            marker.write_text(
                json.dumps(
                    {
                        "delivery_id": "delivery-running",
                        "message_suffix": "read outside workspace",
                        "prior_capture_sha256": hashlib.sha256(prior.encode()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            capture.return_value = (
                "─" * 20
                + "\n> read outside workspace\n● Bash(pwd)\n"
                + "─" * 20
                + "\n>\n"
                + "─" * 20
                + "\nesc to cancel"
            )
            with patch.dict("os.environ", {"OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT": str(root)}):
                self.assertFalse(antigravity_terminal_ready("omnigent://session-agy"))

    @patch("omo_manager.omo_omnigent.request_json")
    def test_binds_first_local_antigravity_conversation(self, request) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root / "bridge"
            conversations = bridge / "agy-home" / ".gemini" / "antigravity-cli" / "conversations"
            conversations.mkdir(parents=True)
            state = bridge / "state.json"
            state.write_text('{"active_turn_id":null,"conversation_id":"agy_conv_pending","session_id":"session-agy"}', encoding="utf-8")
            state.chmod(0o600)
            conversation_id = "8fe265e6-8640-467f-a80c-cfad1611d40a"
            (conversations / f"{conversation_id}.db").touch()
            with patch.dict("os.environ", {"OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT": str(root)}):
                self.assertTrue(bind_local_antigravity_conversation("omnigent://session-agy", wait_s=0))
            self.assertEqual("agy_conv_pending", json.loads(state.read_text(encoding="utf-8"))["conversation_id"])
            request.assert_called_once_with("PATCH", "/v1/sessions/session-agy", {"external_session_id": conversation_id})

    def test_stop_reports_harness_agnostic_resume_command(self) -> None:
        args = StopArgs("omnigent://session-1", 10.0, 80, False, False)
        self.assertEqual("omnigent resume session-1", resume_cmd(args, "session-1"))

    def test_status_mapping(self) -> None:
        self.assertEqual("running", manager_status(SessionSnapshot("s", "running", "codex", True, True)))
        self.assertEqual("ready", manager_status(SessionSnapshot("s", "idle", "codex", True, True)))
        self.assertEqual("missing", manager_status(SessionSnapshot("s", "idle", "codex", False, True)))
        self.assertEqual("missing", manager_status(SessionSnapshot("s", "idle", "codex", None, True)))
        self.assertEqual("missing", manager_status(SessionSnapshot("s", "idle", "codex", False, False)))
        self.assertEqual("missing", manager_status(SessionSnapshot("s", "running", "codex", False, True)))
        self.assertEqual("error", manager_status(SessionSnapshot("s", "failed", "codex", True, True)))

    @patch("omo_manager.omo_omnigent.request_json")
    def test_status_snapshot_requests_fresh_runner_state(self, request) -> None:
        request.return_value = {"id": "session-1", "status": "idle", "harness": "codex", "runner_online": True, "host_online": True}
        self.assertEqual("session-1", session_snapshot("omnigent://session-1").session_id)
        request.assert_called_once_with("GET", "/v1/sessions/session-1?include_items=false&refresh_state=true")

    @patch("omo_manager.omo_omnigent.antigravity_terminal_ready", return_value=True)
    @patch("omo_manager.omo_omnigent.request_json")
    def test_status_treats_ready_antigravity_terminal_as_idle(self, request, _ready) -> None:
        request.return_value = {"id": "session-agy", "status": "running", "harness": "antigravity-native", "runner_online": True, "host_online": True}
        self.assertEqual("idle", session_snapshot("omnigent://session-agy").status)

    @patch("omo_manager.omo_omnigent.request_json")
    def test_message_and_stop_use_session_events(self, request) -> None:
        request.side_effect = [{"queued": True}, {"queued": False}]
        send_message("omnigent://session-1", "hello")
        stop_session("omnigent://session-1")
        self.assertEqual("message", request.call_args_list[0].args[2]["type"])
        self.assertEqual("stop_session", request.call_args_list[1].args[2]["type"])

    def test_message_and_stop_complete_real_http_event_round_trips(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(body)
                events.append((self.path, payload))
                response = json.dumps({"queued": payload["type"] == "message"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.dict("os.environ", {"OMO_MANAGER_OMNIGENT_URL": f"http://127.0.0.1:{server.server_port}"}):
                send_message("omnigent://session-1", "hello")
                stop_session("omnigent://session-1")
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

        self.assertEqual(["/v1/sessions/session-1/events"] * 2, [path for path, _payload in events])
        self.assertEqual(["message", "stop_session"], [payload["type"] for _path, payload in events])

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_uses_actual_harness_agent_and_online_host(self, request) -> None:
        request.side_effect = [
            {"data": [{"id": "agent-codex", "name": "codex-native-ui", "harness": "codex-native"}]},
            {"hosts": [{"host_id": "host-1", "status": "online"}]},
            {"id": "session-1"},
        ]
        target = launch_session("codex", Path("/work"), "gpt-5.6-sol", "high", title="task")
        self.assertEqual("omnigent://session-1", target)
        self.assertEqual(
            call(
                "POST",
                "/v1/sessions",
                {
                    "agent_id": "agent-codex",
                    "host_id": "host-1",
                    "workspace": "/work",
                    "title": "task",
                    "model_override": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "terminal_launch_args": None,
                },
            ),
            request.call_args_list[-1],
        )

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_uses_antigravity_native_agent(self, request) -> None:
        request.side_effect = [
            {"data": [{"id": "agent-agy", "name": "antigravity-native-ui", "harness": "antigravity-native"}]},
            {"hosts": [{"host_id": "host-1", "status": "online"}]},
            {"id": "session-agy"},
        ]
        target = launch_session("antigravity", Path("/work"), "gemini-3.5-flash", "high", title="task")
        self.assertEqual("omnigent://session-agy", target)
        self.assertEqual("agent-agy", request.call_args_list[-1].args[2]["agent_id"])
        self.assertEqual("gemini-3.5-flash", request.call_args_list[-1].args[2]["model_override"])
        self.assertEqual("high", request.call_args_list[-1].args[2]["reasoning_effort"])
        self.assertEqual(
            ["--dangerously-skip-permissions"],
            request.call_args_list[-1].args[2]["terminal_launch_args"],
        )
        self.assertNotIn("labels", request.call_args_list[-1].args[2])

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_uses_cursor_native_agent_with_tmux_equivalent_cli(self, request) -> None:
        request.side_effect = [
            {"data": [{"id": "agent-cursor", "name": "cursor-native-ui", "harness": "cursor-native"}]},
            {"hosts": [{"host_id": "host-1", "status": "online"}]},
            {"id": "session-cursor"},
        ]
        target = launch_session("cursor", Path("/work"), "cursor-grok-4.6", "xhigh", title="task")
        self.assertEqual("omnigent://session-cursor", target)
        payload = request.call_args_list[-1].args[2]
        self.assertEqual("agent-cursor", payload["agent_id"])
        self.assertEqual("cursor-grok-4.6-xhigh", payload["model_override"])
        self.assertEqual("xhigh", payload["reasoning_effort"])
        self.assertEqual(["--force", "--sandbox", "disabled", "--trust"], payload["terminal_launch_args"])
        self.assertNotIn("labels", payload)

    def test_launch_rejects_antigravity_codex_effort_and_flags(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "reasoning effort `low`"):
            launch_session("antigravity", Path("/work"), "gemini-3.5-flash", "xhigh")
        with self.assertRaisesRegex(RuntimeError, "only the exact"):
            launch_session(
                "antigravity",
                Path("/work"),
                "gemini-3.5-flash",
                "high",
                codex_flags=("--dangerously-bypass-approvals-and-sandbox",),
            )

    @patch("omo_manager.omo_omnigent.request_json", return_value={"hosts": []})
    def test_launch_without_online_host_reports_exact_blocker(self, _request) -> None:
        with self.assertRaisesRegex(RuntimeError, "found 0 online hosts"):
            online_host_id(wait_s=0)

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_waits_through_transient_host_offline(self, request) -> None:
        request.side_effect = [
            {"hosts": [{"host_id": "host-1", "status": "offline"}]},
            {"hosts": [{"host_id": "host-1", "status": "online"}]},
        ]
        with patch("omo_manager.omo_omnigent.time.sleep"):
            self.assertEqual("host-1", online_host_id())
        self.assertEqual(2, request.call_count)

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_does_not_wait_when_multiple_hosts_are_online(self, request) -> None:
        request.return_value = {"hosts": [{"host_id": "host-1", "status": "online"}, {"host_id": "host-2", "status": "online"}]}
        with patch("omo_manager.omo_omnigent.time.sleep") as slept, self.assertRaisesRegex(RuntimeError, "found 2 online hosts"):
            online_host_id()
        slept.assert_not_called()
        self.assertEqual(1, request.call_count)

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_rejects_named_agent_with_wrong_underlying_harness(self, request) -> None:
        request.return_value = {"data": [{"id": "agent-codex", "name": "codex-native-ui", "harness": "cursor-native"}]}
        with self.assertRaisesRegex(RuntimeError, "`codex-native` harness"):
            launch_session("codex", Path("/work"), "gpt-5.6-sol", "high")

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_portably_passes_exact_full_access_opt_in(self, request) -> None:
        request.side_effect = [
            {"data": [{"id": "agent-codex", "name": "codex-native-ui", "harness": "codex-native"}]},
            {"hosts": [{"host_id": "host-1", "status": "online"}]},
            {"id": "session-1"},
        ]
        launch_session(
            "codex",
            Path("/work"),
            "gpt-6-astra",
            "low",
            codex_flags=("--dangerously-bypass-approvals-and-sandbox",),
        )
        payload = request.call_args_list[-1].args[2]
        self.assertEqual({"omnigent.codex_native.bypass_sandbox": "1"}, payload["labels"])
        self.assertIsNone(payload["terminal_launch_args"])

    def test_launch_rejects_other_or_duplicate_raw_flags(self) -> None:
        for flags in (("--profile",), ("--dangerously-bypass-approvals-and-sandbox",) * 2):
            with self.subTest(flags=flags), self.assertRaisesRegex(RuntimeError, "only the exact"):
                launch_session("codex", Path("/work"), "gpt-6-astra", "low", codex_flags=flags)

    @patch("omo_manager.omo_omnigent.request_json")
    def test_session_ready_report_uses_newest_assistant_item(self, request) -> None:
        request.return_value = {
            "data": [
                {"id": "u2", "type": "message", "role": "user", "status": "completed", "content": [{"text": "docs mention omo_report.sh"}]},
                {"id": "a1", "type": "message", "role": "assistant", "status": "completed", "content": [{"text": "run omo_report.sh next"}]},
                {"id": "t1", "type": "function_call", "status": "completed", "arguments": "omo_report.sh --status in-progress"},
                {"id": "u1", "type": "message", "role": "user", "status": "completed", "content": [{"text": "hi"}]},
            ]
        }
        self.assertEqual(("a1", True), session_ready_report("omnigent://session-1"))
        request.return_value = {
            "data": [
                {"id": "a1", "type": "message", "role": "assistant", "status": "completed", "content": [{"text": "run omo_report.sh"}]},
            ]
        }
        self.assertEqual(("a1", False), session_ready_report("omnigent://session-1"))
        request.return_value = {"data": [{"id": "u1", "type": "message", "role": "user", "status": "completed", "content": [{"text": "hi"}]}]}
        with patch("omo_manager.omo_omnigent.antigravity_terminal_capture", return_value=None):
            self.assertIsNone(session_ready_report("omnigent://session-1"))

    @patch("omo_manager.omo_omnigent._expected_antigravity_turn", return_value=("> hello\nanswer", "delivery-2"))
    @patch("omo_manager.omo_omnigent.antigravity_terminal_capture", return_value="capture")
    @patch("omo_manager.omo_omnigent.request_json", return_value={"data": []})
    def test_session_ready_report_prefers_antigravity_terminal_when_api_items_are_empty(self, _request, _capture, _expected) -> None:
        item_id, invoked_report = session_ready_report("omnigent://session-agy") or ("", True)
        self.assertEqual(64, len(item_id))
        self.assertFalse(invoked_report)

    @patch("omo_manager.omo_omnigent.antigravity_terminal_capture")
    @patch("omo_manager.omo_omnigent.request_json")
    def test_session_ready_report_prefers_fresh_antigravity_terminal_over_stale_item(self, request, capture) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root / "bridge"
            bridge.mkdir()
            state = bridge / "state.json"
            state.write_text('{"conversation_id":"conversation","session_id":"session-agy"}', encoding="utf-8")
            state.chmod(0o600)
            prior = "─" * 20 + "\n> prior prompt\nprior answer\n" + "─" * 20 + "\n>\n" + "─" * 20 + "\n? for shortcuts"
            turn = "> do not run omo_report.sh; finish normally\nfresh answer"
            capture.return_value = "─" * 20 + f"\n{turn}\n" + "─" * 20 + "\n>\n" + "─" * 20 + "\n? for shortcuts"
            marker = bridge / "omo-manager-expected-turn.json"
            marker.write_text(
                json.dumps(
                    {
                        "delivery_id": "delivery-new",
                        "message_suffix": "do not run omo_report.sh; finish normally",
                        "prior_capture_sha256": hashlib.sha256(prior.encode()).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )
            marker.chmod(0o600)
            request.return_value = {"data": [{"id": "stale", "type": "message", "role": "assistant", "status": "completed"}]}
            with patch.dict("os.environ", {"OMO_MANAGER_OMNIGENT_AGY_BRIDGE_ROOT": str(root)}):
                item_id, invoked_report = session_ready_report("omnigent://session-agy") or ("", True)
        self.assertEqual(hashlib.sha256(f"delivery-new\0{turn}".encode()).hexdigest(), item_id)
        self.assertFalse(invoked_report)
        request.assert_not_called()

    @patch("omo_manager.omo_omnigent._expected_antigravity_turn", side_effect=[("> repeat\nanswer", "delivery-1"), ("> repeat\nanswer", "delivery-2")])
    @patch("omo_manager.omo_omnigent.antigravity_terminal_capture", return_value="capture")
    @patch("omo_manager.omo_omnigent.request_json", return_value={"data": []})
    def test_identical_antigravity_turns_keep_distinct_ready_ids(self, _request, _capture, _expected) -> None:
        first = session_ready_report("omnigent://session-agy")
        second = session_ready_report("omnigent://session-agy")
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
