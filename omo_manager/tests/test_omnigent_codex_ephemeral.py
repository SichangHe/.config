"""Exercise installed native forwarding with realistic thread notifications."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

HAS_OMNIGENT = importlib.util.find_spec("omnigent") is not None
if HAS_OMNIGENT:
    import httpx
    from omnigent import codex_native_forwarder as forwarder
    from omnigent.codex_native_app_server import CodexAppServerClient, CodexMessage


def started(thread_id: str, **metadata: object) -> CodexMessage:
    return {
        "jsonrpc": "2.0",
        "method": "thread/started",
        "params": {"thread": {"id": thread_id, "source": "cli", **metadata}},
    }


@unittest.skipUnless(HAS_OMNIGENT, "OmniGent is not installed in this interpreter")
class NativeEphemeralThreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"agent_id": "agent_main", "runner_id": "runner_main", "labels": {}})
            if request.url.path == "/v1/sessions":
                return httpx.Response(200, json={"id": "conv_new"})
            return httpx.Response(200, json={})

        self.ap_client = await self.enterAsyncContext(
            httpx.AsyncClient(base_url="http://omnigent.test", transport=httpx.MockTransport(respond))
        )
        self.target = forwarder._ForwarderTarget(
            session_id="conv_main",
            thread_id="thread_main",
            delta_coalescer=forwarder._OutputTextDeltaCoalescer(self.ap_client, "conv_main"),
            usage_coalescer=forwarder._SessionUsageCoalescer(self.ap_client, "conv_main"),
            elicitation_tracker=forwarder._CodexElicitationTaskTracker(),
        )

    async def asyncTearDown(self) -> None:
        await self.target.delta_coalescer.close()
        await self.target.usage_coalescer.close()
        await self.target.elicitation_tracker.close()

    async def discover(self, *events: CodexMessage) -> str:
        client = CodexAppServerClient(ws_url="ws://codex.test")
        for event in events:
            await client._events.put(event)
        return await forwarder.wait_for_thread_started(client, timeout=0.2)

    async def rotate(self, event: CodexMessage) -> bool:
        return await forwarder._maybe_rotate_session_on_thread_started(
            ap_client=self.ap_client,
            target=self.target,
            bridge_dir=Path("/unused-test-bridge"),
            app_server_url="ws://codex.test",
            event=event,
        )

    async def test_initial_discovery_ignores_ephemeral_before_primary(self) -> None:
        result = await self.discover(
            started("thread_title", ephemeral=True),
            {"jsonrpc": "2.0", "method": "thread/name/updated", "params": {"threadId": "thread_title", "threadName": "Task title"}},
            started("thread_main", ephemeral=False),
        )
        self.assertEqual(result, "thread_main")

    async def test_ephemeral_rotation_preserves_target_without_http(self) -> None:
        before = vars(self.target).copy()
        self.assertFalse(await self.rotate(started("thread_title", ephemeral=True)))
        self.assertEqual(vars(self.target), before)
        self.assertEqual(self.requests, [])

    async def test_subagent_discovery_and_rotation_preserve_parent(self) -> None:
        child = started("thread_child", ephemeral=False, source={"subAgent": {"thread_spawn": {"parent_thread_id": "thread_main"}}})
        self.assertEqual(await self.discover(child, started("thread_main")), "thread_main")
        before = vars(self.target).copy()
        self.assertFalse(await self.rotate(child))
        self.assertEqual(vars(self.target), before)
        self.assertEqual(self.requests, [])

    async def test_missing_and_unknown_ephemeral_remain_discoverable(self) -> None:
        for metadata in ({}, {"ephemeral": False}, {"ephemeral": None}, {"ephemeral": "true"}, {"ephemeral": 1}):
            with self.subTest(metadata=metadata):
                self.assertEqual(await self.discover(started("thread_primary", **metadata)), "thread_primary")

    async def test_native_clear_still_rotates_with_compatible_metadata(self) -> None:
        for metadata in ({}, {"ephemeral": False}, {"ephemeral": None}, {"ephemeral": "true"}, {"ephemeral": 1}):
            with self.subTest(metadata=metadata):
                self.target.session_id = "conv_main"
                self.target.thread_id = "thread_main"
                self.requests.clear()
                old_delta = self.target.delta_coalescer
                old_usage = self.target.usage_coalescer
                old_elicitation = self.target.elicitation_tracker
                with patch.object(forwarder, "read_bridge_state", return_value=None), patch.object(forwarder, "write_bridge_state") as write_state:
                    self.assertTrue(await self.rotate(started("thread_clear", **metadata)))
                self.assertEqual((self.target.session_id, self.target.thread_id), ("conv_new", "thread_clear"))
                self.assertIsNot(self.target.delta_coalescer, old_delta)
                self.assertIsNot(self.target.usage_coalescer, old_usage)
                self.assertIsNot(self.target.elicitation_tracker, old_elicitation)
                self.assertEqual([(request.method, request.url.path) for request in self.requests], [
                    ("GET", "/v1/sessions/conv_main"),
                    ("POST", "/v1/sessions"),
                    ("PATCH", "/v1/sessions/conv_new"),
                    ("PATCH", "/v1/sessions/conv_new"),
                    ("POST", "/v1/sessions/conv_main/resources/terminals/terminal_codex_main/transfer"),
                    ("PATCH", "/v1/sessions/conv_main"),
                ])
                self.assertEqual(json.loads(self.requests[3].content), {"external_session_id": "thread_clear"})
                self.assertEqual(json.loads(self.requests[4].content), {"target_session_id": "conv_new"})
                self.assertEqual(json.loads(self.requests[5].content), {"runner_id": ""})
                write_state.assert_called_once()
                state = write_state.call_args.args[1]
                self.assertEqual((state.session_id, state.thread_id, state.socket_path), ("conv_new", "thread_clear", "ws://codex.test"))


if __name__ == "__main__":
    unittest.main()
