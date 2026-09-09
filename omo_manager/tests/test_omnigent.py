from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import call, patch

from omo_manager.omo_codex_stop import Args as StopArgs
from omo_manager.omo_codex_stop import resume_cmd
from omo_manager.omo_omnigent import SessionSnapshot, launch_session, manager_status, send_message, session_snapshot, stop_session


class OmniGentRuntimeTests(unittest.TestCase):
    def test_stop_reports_harness_agnostic_resume_command(self) -> None:
        args = StopArgs("omnigent://session-1", 10.0, 80, False, False)
        self.assertEqual("omnigent resume session-1", resume_cmd(args, "session-1"))

    def test_status_mapping(self) -> None:
        self.assertEqual("running", manager_status(SessionSnapshot("s", "running", "codex", True, True)))
        self.assertEqual("ready", manager_status(SessionSnapshot("s", "idle", "codex", False, True)))
        self.assertEqual("missing", manager_status(SessionSnapshot("s", "idle", "codex", False, False)))
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

    @patch("omo_manager.omo_omnigent.request_json")
    def test_launch_uses_actual_harness_agent_and_online_host(self, request) -> None:
        request.side_effect = [
            {"data": [{"id": "agent-codex", "name": "codex-native-ui"}]},
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


if __name__ == "__main__":
    unittest.main()
