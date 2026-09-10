from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import call, patch

from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import resume_cmd
from omo_manager.omo_omnigent import SessionSnapshot, launch_session, manager_status, online_host_id, send_message, session_snapshot, stop_session


class OmniGentRuntimeTests(unittest.TestCase):
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
                },
            ),
            request.call_args_list[-1],
        )

    @patch("omo_manager.omo_omnigent.request_json", return_value={"hosts": []})
    def test_launch_without_online_host_reports_exact_blocker(self, _request) -> None:
        with self.assertRaisesRegex(RuntimeError, "found 0 online hosts"):
            online_host_id()

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_rejects_named_agent_with_wrong_underlying_harness(self, request) -> None:
        request.return_value = {"data": [{"id": "agent-codex", "name": "codex-native-ui", "harness": "cursor-native"}]}
        with self.assertRaisesRegex(RuntimeError, "`codex-native` harness"):
            launch_session("codex", Path("/work"), "gpt-5.6-sol", "high")


if __name__ == "__main__":
    unittest.main()
