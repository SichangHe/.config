"""Exercise native ingress with installed runner clients and real process pins."""
# pyright: reportUninitializedInstanceVariable=false

from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread
from typing import TypedDict
from unittest.mock import patch

import httpx
import uvicorn
from omnigent.cli_auth import open_server_client
from omnigent.codex_native_forwarder import (
    _post_agent_message,
    _post_external_session_todos,
    _post_mcp_startup,
    _post_output_reasoning_delta,
    _post_output_text_delta,
    _post_session_event,
    _post_session_interrupted,
    _post_status,
    _post_tool_call_item,
    _post_tool_output_delta,
    _post_user_message,
    _session_usage_data_from_params,
)
from omnigent.runner import _entry
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import UnifiedAuthProvider
from omo_manager.omo_omnigent_fence import FenceGuard, FenceStore, Operation, Rejected, ReplacementSpec, _boot_id, _process_start, _runner_context, digest
from omo_manager.omo_omnigent_ingress import TOKEN_PREFIX, NativeIngress
from omo_manager.tests.test_omnigent_fence import AUTHORITY, EVIDENCE, REQUEST, expected_state, phase_receipt
from starlette.applications import Starlette
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


class CapturedCall(TypedDict):
    method: str
    path: str
    payload: object
    headers: dict[str, str]
    runner_id: str | None


class OmnigentIngressTests(unittest.TestCase):
    """Only the loopback fixture receives requests; no live runner is launched."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="omnigent-ingress-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = FenceStore(self.directory / "fence.sqlite")
        self.spec = ReplacementSpec("replace-ingress", "new-session", digest(AUTHORITY), digest(REQUEST), expected_state())
        self.binding_token = secrets.token_urlsafe(32)
        self.runner_id = token_bound_runner_id(self.binding_token)
        self.binding_headers = {RUNNER_TUNNEL_TOKEN_HEADER: self.binding_token, "Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
        self.root = f"/v1/sessions/{self.spec.new_session_id}"
        self.mint_path = f"/v1/runners/{self.runner_id}/token"
        self.now_s = time.time()
        self.ready = True
        self.server_endpoint = ("127.0.0.1", 18080)
        self.base_url = "http://127.0.0.1:18080"
        self.native = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_native)
        self.pin: dict[str, object] = {
            "session_id": self.spec.new_session_id,
            "runner_id": self.runner_id,
            "owner": "local",
            "thread_id": "native-thread",
            "pid": self.native.pid,
            "start_ticks": _process_start(self.native.pid),
            "boot_id": _boot_id(),
            "argv_sha256": digest(Path(f"/proc/{self.native.pid}/cmdline").read_bytes()),
            "host_pid": os.getpid(),
            "host_start_ticks": _process_start(os.getpid()),
            "host_id": self.spec.expected.host_id,
        }
        self.assertIsInstance(self.store.prepare(self.spec, self.spec.expected, AUTHORITY, REQUEST), Operation)
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.spec.expected), Operation)
        self.advance("old_quiesced")
        self.assertIsInstance(self.store.claim_successor_launch(self.spec.expected.host_id, self.spec.new_session_id, self.runner_id, "launch-once"), Operation)
        self.advance("new_prepared")
        self.advance("committed")
        self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
        self.ingress = self.make_ingress()
        self.calls: list[CapturedCall] = []
        self.application = Starlette(routes=[Route("/{path:path}", self.endpoint, methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"])])
        self.guard = FenceGuard(self.application, self.store, ingress=self.ingress)
        self.client = self.enterContext(TestClient(self.guard, base_url=self.base_url))

    def stop_native(self) -> None:
        if self.native.poll() is None:
            self.native.terminate()
        self.native.wait(timeout=5)

    def advance(self, phase: str) -> None:
        self.assertIsInstance(self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase)), Operation)

    def claim_manifest(self) -> None:
        thread_id = self.pin["thread_id"]
        assert isinstance(thread_id, str)
        self.assertIsInstance(
            self.store.record_evidence(self.spec.operation_id, "native_successor_verified", {"operation_id": self.spec.operation_id, "evidence_sha256": EVIDENCE, "thread_id": self.pin["thread_id"]}),
            Operation,
        )
        self.assertIsInstance(self.store.claim_manifest_dispatch(self.spec.operation_id, "delivery-once", digest(REQUEST), self.runner_id, thread_id), Operation)

    def make_ingress(self) -> NativeIngress:
        provider = UnifiedAuthProvider(source="header", local_single_user=True, header_name="X-Forwarded-Email", header_strip_prefix="")
        return NativeIngress(
            self.store,
            endpoint=self.server_endpoint,
            resolve_owner=lambda _: "local",
            resolve_user=lambda scope: provider.get_user_id(HTTPConnection(scope)),
            resolve_native_pin=lambda _: self.pin.copy() if self.ready else Rejected("native_not_ready", "fixture has no native process yet"),
            clock=lambda: self.now_s,
            ttl_s=60,
        )

    async def endpoint(self, request: Request) -> JSONResponse:
        body = await request.body()
        payload = await request.json() if body else None
        self.calls.append({"method": request.method, "path": request.url.path, "payload": payload, "headers": dict(request.headers), "runner_id": _runner_context.get()})
        if request.url.path.endswith("/token"):
            return JSONResponse({"error": "stock local auth cannot mint"}, status_code=400)
        if request.url.path.endswith("/items"):
            return JSONResponse({"data": [], "has_more": False})
        return JSONResponse({"labels": {}, "external_session_id": None, "ok": True})

    def mint(self) -> str:
        response = self.client.post(self.mint_path, headers=self.binding_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def authorization(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.mint()}"}

    def user_item(self) -> dict[str, object]:
        return {
            "type": "external_conversation_item",
            "data": {"item_type": "message", "item_data": {"role": "user", "content": [{"type": "input_text", "text": REQUEST.decode()}]}, "response_id": "codex_turn-fixture"},
        }

    def tool_item(self) -> dict[str, object]:
        return {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {"agent": "codex-native-ui", "name": "exec_command", "arguments": '{"cmd":"true"}', "call_id": "call-fixture"},
                "response_id": "codex_turn-fixture",
            },
        }

    def pending(self) -> list[dict[str, str]]:
        with self.store._transaction() as db:
            return [dict(row) for row in db.execute("SELECT * FROM ingress_pending")]

    async def await_pending(self) -> dict[str, str]:
        deadline_s = time.monotonic() + 1
        while time.monotonic() < deadline_s:
            rows = self.pending()
            if rows:
                self.assertEqual(rows[0]["status"], "pending")
                return rows[0]
            await asyncio.sleep(0.005)
        self.fail("native tool callback was not durably deferred")

    def test_configuration_requires_exact_ipv4_loopback_and_valid_port(self) -> None:
        original = self.server_endpoint
        for endpoint in (("192.0.2.10", 18080), ("0.0.0.0", 18080), ("127.0.0.2", 18080), ("localhost", 18080), ("::1", 18080), ("127.0.0.1", 0), ("127.0.0.1", 65536)):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.server_endpoint = endpoint
                self.make_ingress()
        self.server_endpoint = original

    def test_wrong_socket_endpoint_cannot_mint_read_or_use_capability(self) -> None:
        token = self.mint()
        claimed_host = {"Host": "127.0.0.1:18080"}
        for url in ("http://192.0.2.10:18080", "http://127.0.0.1:18081", "http://localhost:18080"):
            with self.subTest(url=url), TestClient(self.guard, base_url=url) as client:
                requests = (
                    client.post(self.mint_path, headers=self.binding_headers | claimed_host),
                    client.get(self.root, headers=self.binding_headers | claimed_host),
                    client.patch(self.root, headers=self.authorization(token) | claimed_host, json={"external_session_id": self.pin["thread_id"]}),
                    client.post(self.root + "/events", headers=self.authorization(token) | claimed_host, json=self.tool_item()),
                )
                for response in requests:
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json()["error"], "native_endpoint_mismatch")
        self.assertEqual(self.calls, [])

    def test_host_header_does_not_select_the_socket_endpoint(self) -> None:
        headers = self.binding_headers | {"Host": "192.0.2.10:18081"}
        response = self.client.post(self.mint_path, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["token"]
        response = self.client.patch(self.root, headers=self.authorization(token) | {"Host": "192.0.2.10:18081"}, json={"external_session_id": self.pin["thread_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.calls), 1)

    def test_capability_remains_bound_to_original_endpoint_after_reconfiguration(self) -> None:
        token = self.mint()
        self.ingress.endpoint = ("127.0.0.1", 18081)
        response = self.client.patch(self.root, headers=self.authorization(token), json={"external_session_id": self.pin["thread_id"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "native_endpoint_mismatch")
        with TestClient(self.guard, base_url="http://127.0.0.1:18081") as client:
            for response in (
                client.post(self.mint_path, headers=self.binding_headers),
                client.patch(self.root, headers=self.authorization(token), json={"external_session_id": self.pin["thread_id"]}),
            ):
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"], "native_generation_changed")
        self.assertEqual(self.calls, [])

    def test_foreign_or_reserved_identity_header_cannot_override_local_owner(self) -> None:
        token = self.mint()
        for identity in ("foreign@example.invalid", "local"):
            with self.subTest(identity=identity):
                foreign = {"X-Forwarded-Email": identity}
                for response in (
                    self.client.post(self.mint_path, headers=self.binding_headers | foreign),
                    self.client.get(self.root, headers=self.binding_headers | foreign),
                    self.client.patch(self.root, headers=self.authorization(token) | foreign, json={"external_session_id": self.pin["thread_id"]}),
                    self.client.patch(self.root, headers=self.authorization(token) | self.binding_headers | foreign, json={"external_session_id": self.pin["thread_id"]}),
                ):
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["error"], "native_owner_auth_unavailable")
                response = self.client.post(self.root + "/events", headers=self.authorization(token) | self.binding_headers | foreign, json=self.tool_item())
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"], "native_deferred_stock_auth_denied")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.client.patch(self.root, headers=self.authorization(token), json={"external_session_id": self.pin["thread_id"]}).status_code, 200)

    def test_placeholder_has_no_reserved_authority_and_binding_reads_are_exact(self) -> None:
        self.ready = False
        placeholder = self.mint()
        self.assertFalse(placeholder.startswith(TOKEN_PREFIX))
        for suffix in ("", "/labels", "/agent/contents", "/items?limit=1&order=desc"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.client.get(self.root + suffix, headers=self.authorization(placeholder)).status_code, 409)
                self.assertEqual(self.client.get(self.root + suffix, headers=self.binding_headers).status_code, 200)
        for suffix in ("?refresh_state=true", "/items?limit=2&order=desc", "/labels?refresh=true", "/settings"):
            with self.subTest(suffix=suffix):
                self.assertGreaterEqual(self.client.get(self.root + suffix, headers=self.binding_headers).status_code, 400)
        self.assertGreaterEqual(self.client.patch(self.root, headers=self.authorization(placeholder), json={"external_session_id": self.pin["thread_id"]}).status_code, 400)
        self.assertEqual(self.client.post(self.root + "/events", headers=self.binding_headers, json=self.user_item()).status_code, 401)
        self.assertEqual(len(self.calls), 4)

    def test_capability_cannot_dispatch_or_escape_exact_session_method_and_body(self) -> None:
        token = self.mint()
        cases = (
            ("GET", self.root, None),
            ("PATCH", self.root, {"external_session_id": "wrong-thread"}),
            ("PATCH", self.root, {"external_session_id": self.pin["thread_id"], "labels": {"omnigent.ui": "terminal"}}),
            ("POST", self.root + "/messages", {"role": "user", "content": "dispatch"}),
            ("POST", self.root + "/events", {"type": "message", "data": {"role": "user", "content": "dispatch"}}),
            ("POST", self.root + "/events", {"type": "retry", "data": {}}),
            ("POST", self.root + "/events", {"type": "external_session_status", "data": {"status": "running", "control": {"type": "retry"}}}),
            ("POST", self.root + "/events?session_id=unrelated", {"type": "external_session_status", "data": {"status": "idle"}}),
            ("POST", "/v1/sessions/unrelated/events", self.user_item()),
            ("POST", f"/v1/sessions/{self.spec.expected.session_id}/events", self.user_item()),
            ("POST", "/v1/hosts/fixture-host/runners", {"session_id": self.spec.new_session_id}),
        )
        for method, path, body in cases:
            with self.subTest(method=method, path=path, body=body):
                response = self.client.request(method, path, headers=self.authorization(token), json=body)
                self.assertGreaterEqual(response.status_code, 400, response.text)
        self.assertEqual(self.calls, [])

    def test_wrong_tokens_runner_and_duplicate_credentials_never_reach_application(self) -> None:
        token = self.mint()
        body = {"type": "external_session_status", "data": {"status": "idle"}}
        headers = (
            {"Authorization": "Bearer " + TOKEN_PREFIX + "unknown"},
            {"Authorization": "Bearer shared-owner-token"},
            self.authorization(token) | {RUNNER_TUNNEL_TOKEN_HEADER: "different-runner-binding"},
            [("Authorization", f"Bearer {token}"), ("Authorization", "Bearer second")],
            [(RUNNER_TUNNEL_TOKEN_HEADER, self.binding_token), (RUNNER_TUNNEL_TOKEN_HEADER, self.binding_token)],
        )
        for credentials in headers:
            with self.subTest(kind=type(credentials).__name__):
                self.assertGreaterEqual(self.client.post(self.root + "/events", headers=credentials, json=body).status_code, 400)
        self.assertGreaterEqual(self.client.post("/v1/runners/wrong-runner/token", headers=self.binding_headers).status_code, 400)
        self.assertGreaterEqual(self.client.get(self.mint_path, headers=self.binding_headers).status_code, 400)
        self.assertGreaterEqual(self.client.post(self.mint_path, headers=self.binding_headers, json={}).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_only_the_claimed_prompt_can_be_mirrored_before_confirmation(self) -> None:
        self.claim_manifest()
        variants = (
            {"role": "user", "content": [{"type": "input_text", "text": "different prompt"}]},
            {"role": "user", "content": [{"type": "input_text", "text": REQUEST.decode()}], "control": {"type": "retry"}},
            {"role": "user", "content": [{"type": "input_text", "text": REQUEST.decode(), "session_id": "unrelated"}]},
            {"role": "user", "content": [{"type": "input_text", "text": REQUEST.decode()}, {"type": "input_text", "text": "extra"}]},
        )
        for item in variants:
            body = self.user_item()
            data = body["data"]
            assert isinstance(data, dict)
            data["item_data"] = item
            self.assertGreaterEqual(self.client.post(self.root + "/events", headers=self.authorization(), json=body).status_code, 400)
        for item_type in ("tool_call", "tool_result", "function_call", "function_call_output"):
            body = self.user_item()
            data = body["data"]
            assert isinstance(data, dict)
            data["item_type"] = item_type
            self.assertGreaterEqual(self.client.post(self.root + "/events", headers=self.authorization(), json=body).status_code, 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post(self.root + "/events", headers=self.authorization(), json=self.user_item()).status_code, 200)

    def test_single_use_and_durable_callback_replay_rejection(self) -> None:
        self.claim_manifest()
        token = self.mint()
        self.assertEqual(self.client.post(self.root + "/events", headers=self.authorization(token), json=self.user_item()).status_code, 200)
        for headers in (self.authorization(token), self.authorization()):
            response = self.client.post(self.root + "/events", headers=headers, json=self.user_item())
            self.assertEqual(response.status_code, 409)
            self.assertIn("replay", response.json()["error"])
        self.assertEqual(len(self.calls), 1)

    def test_expiry_process_death_and_server_refresh_fail_closed(self) -> None:
        self.claim_manifest()
        token = self.mint()
        self.now_s += 61
        response = self.client.post(self.root + "/events", headers=self.authorization(token), json=self.user_item())
        self.assertEqual(response.json()["error"], "native_token_expired")
        token = self.mint()
        refreshed = self.make_ingress()
        with TestClient(FenceGuard(self.application, self.store, ingress=refreshed), base_url=self.base_url) as client:
            self.assertEqual(client.post(self.root + "/events", headers=self.authorization(token), json=self.user_item()).status_code, 409)
            self.assertEqual(client.post(self.mint_path, headers=self.binding_headers).status_code, 409)
        self.stop_native()
        self.assertEqual(self.client.post(self.root + "/events", headers=self.authorization(token), json=self.user_item()).status_code, 409)
        self.assertEqual(self.client.post(self.mint_path, headers=self.binding_headers).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_replacement_server_pid_cannot_mint_from_prior_launch_claim(self) -> None:
        code = """
import json
import sys
from pathlib import Path
from omo_manager.omo_omnigent_fence import FenceStore, IngressReply
from omo_manager.omo_omnigent_ingress import BINDING_HEADER, NativeIngress

def native_pin(operation):
    raise AssertionError("server-generation rejection must precede native resolution")

ingress = NativeIngress(
    FenceStore(Path(sys.argv[1])), resolve_owner=lambda _: "local",
    resolve_user=lambda _: "local", endpoint=("127.0.0.1", 80),
    resolve_native_pin=native_pin,
)
reply = ingress.handle({"type": "http", "method": "POST", "path": sys.argv[3],
    "server": ("127.0.0.1", 80), "query_string": b"", "headers": [(BINDING_HEADER, sys.argv[2].encode())]}, None)
assert isinstance(reply, IngressReply)
print(json.dumps({"status": reply.status, "error": reply.body["error"]}))
"""
        result = subprocess.run([sys.executable, "-c", code, str(self.directory / "fence.sqlite"), self.binding_token, self.mint_path], capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(json.loads(result.stdout), {"status": 409, "error": "native_server_generation_changed"})
        self.assertEqual(self.calls, [])

    def test_concurrent_duplicate_callback_has_one_downstream_write(self) -> None:
        self.claim_manifest()
        tokens = [self.mint(), self.mint()]
        barrier = Barrier(2)

        def post(token: str) -> int:
            with TestClient(self.guard, base_url=self.base_url) as client:
                barrier.wait(timeout=5)
                return client.post(self.root + "/events", headers=self.authorization(token), json=self.user_item()).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(post, tokens))
        self.assertEqual(sorted(statuses), [200, 409])
        self.assertEqual(len(self.calls), 1)

    def test_exact_prompt_cannot_be_mirrored_before_manifest_dispatch_claim(self) -> None:
        self.assertEqual(self.client.post(self.root + "/events", headers=self.authorization(), json=self.user_item()).status_code, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.patch(self.root, headers=self.authorization(), json={"external_session_id": self.pin["thread_id"]}).status_code, 200)
        self.assertEqual(self.client.post(self.root + "/events", headers=self.authorization(), json={"type": "external_session_status", "data": {"status": "idle"}}).status_code, 200)

    def test_real_tool_callback_waits_for_independent_confirmation_without_grant(self) -> None:
        self.claim_manifest()
        user_headers, tool_headers = self.authorization(), self.authorization()

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.guard), base_url=self.base_url) as client:
                client.headers.update(user_headers)
                await _post_user_message(
                    client, self.spec.new_session_id, {"turnId": "turn-fixture"}, {"id": "user-fixture", "type": "userMessage", "content": [{"type": "text", "text": REQUEST.decode()}]}
                )
                self.assertEqual(len(self.calls), 1)
                client.headers.update(tool_headers)
                task = asyncio.create_task(_post_tool_call_item(client, self.spec.new_session_id, {"turnId": "turn-fixture"}, {"id": "call-fixture", "type": "commandExecution", "command": "true"}))
                pending = await self.await_pending()
                self.assertFalse(task.done())
                self.assertEqual(len(self.calls), 1)
                saved = json.loads(pending["payload"])
                self.assertEqual(saved["method"], "POST")
                self.assertEqual(saved["path"], self.root + "/events")
                self.assertNotIn(tool_headers["Authorization"], pending["payload"])
                independent = FenceStore(self.directory / "fence.sqlite")
                result = independent.advance(self.spec.operation_id, "delivery_confirmed", phase_receipt(self.spec, "delivery_confirmed"))
                self.assertIsInstance(result, Operation)
                self.assertEqual(await asyncio.wait_for(task, 2), "call-fixture")
                self.assertEqual(self.calls[-1]["payload"], saved["body"])

        asyncio.run(exercise())
        self.assertEqual(self.pending()[0]["status"], "released")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["runner_id"], self.runner_id)
        self.assertIsNone(self.calls[1]["runner_id"])
        self.assertEqual(self.calls[1]["headers"]["authorization"], tool_headers["Authorization"])
        self.assertEqual(self.store.active_admissions(self.spec.new_session_id), ())
        body = self.calls[1]["payload"]
        assert isinstance(body, dict)
        for headers in ({}, self.authorization()):
            response = self.client.post(self.root + "/events", headers=headers, json=body)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"], "native_callback_replayed")
        data = body["data"]
        assert isinstance(data, dict)
        changed = body | {"data": data | {"response_id": "codex_new-turn"}}
        self.assertEqual(self.client.post(self.root + "/events", json=changed).status_code, 200)
        self.assertEqual(len(self.calls), 3)
        self.assertIsNone(self.calls[-1]["runner_id"])

    def test_deferred_tool_requires_stock_auth(self) -> None:
        headers = self.authorization()
        self.ingress.resolve_user = lambda _: None
        response = self.client.post(self.root + "/events", headers=headers, json=self.tool_item())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "native_deferred_stock_auth_denied")
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.calls, [])

    def test_deferred_timeout_keeps_payload_uncertain_and_cannot_be_replayed(self) -> None:
        self.ingress.wait_timeout_s = 0.03
        headers = self.authorization()
        response = self.client.post(self.root + "/events", headers=headers, json=self.tool_item())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "native_deferred_confirmation_timeout")
        self.assertEqual(self.pending()[0]["status"], "uncertain")
        self.assertEqual(json.loads(self.pending()[0]["payload"])["body"], self.tool_item())
        for credentials in (headers, self.authorization()):
            self.assertEqual(self.client.post(self.root + "/events", headers=credentials, json=self.tool_item()).status_code, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.active_admissions(self.spec.new_session_id), ())
        independent = FenceStore(self.directory / "fence.sqlite")
        self.assertIsInstance(independent.advance(self.spec.operation_id, "delivery_confirmed", phase_receipt(self.spec, "delivery_confirmed")), Operation)
        for credentials in ({}, self.authorization()):
            response = self.client.post(self.root + "/events", headers=credentials, json=self.tool_item())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"], "native_callback_replayed")
        self.assertEqual(self.pending()[0]["status"], "uncertain")
        self.assertEqual(self.calls, [])

    def test_native_crash_while_tool_waits_keeps_uncertain_payload(self) -> None:
        headers = self.authorization()

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.guard), base_url=self.base_url) as client:
                task = asyncio.create_task(client.post(self.root + "/events", headers=headers, json=self.tool_item()))
                await self.await_pending()
                self.stop_native()
                response = await asyncio.wait_for(task, 2)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["error"], "native_deferred_generation_changed")

        asyncio.run(exercise())
        self.assertEqual(self.pending()[0]["status"], "uncertain")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.active_admissions(self.spec.new_session_id), ())

    def test_deferred_expiry_is_checked_during_wait(self) -> None:
        self.exercise_deferred_failure("expiry")

    def test_deferred_stock_auth_is_rechecked_after_confirmation(self) -> None:
        self.exercise_deferred_failure("stock_auth")

    def test_endpoint_change_while_tool_waits_keeps_uncertain_payload(self) -> None:
        self.exercise_deferred_failure("endpoint")

    def test_cancelled_request_retains_uncertain_payload_across_ingress_refresh(self) -> None:
        self.exercise_deferred_failure("cancel")

    def exercise_deferred_failure(self, failure: str) -> None:
        headers = self.authorization()

        async def exercise() -> None:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.guard), base_url=self.base_url) as client:
                task = asyncio.create_task(client.post(self.root + "/events", headers=headers, json=self.tool_item()))
                await self.await_pending()
                if failure == "cancel":
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    if failure == "expiry":
                        self.now_s += 61
                    elif failure == "endpoint":
                        self.ingress.endpoint = ("127.0.0.1", 18081)
                    else:
                        self.ingress.resolve_user = lambda _: None
                        self.advance("delivery_confirmed")
                    response = await asyncio.wait_for(task, 2)
                    self.assertEqual(response.status_code, 409)
                    expected_error = {"expiry": "native_deferred_expired_or_replayed", "endpoint": "native_deferred_generation_changed", "stock_auth": "native_deferred_stock_auth_denied"}
                    self.assertEqual(response.json()["error"], expected_error[failure])

        asyncio.run(exercise())
        pending = self.pending()[0]
        self.assertEqual(pending["status"], "uncertain")
        self.assertEqual(json.loads(pending["payload"])["body"], self.tool_item())
        self.assertNotIn(headers["Authorization"].removeprefix("Bearer "), json.dumps(pending))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.active_admissions(self.spec.new_session_id), ())
        with TestClient(FenceGuard(self.application, self.store, ingress=self.make_ingress()), base_url=self.base_url) as client:
            self.assertEqual(client.post(self.root + "/events", headers=headers, json=self.tool_item()).status_code, 409)
        self.assertEqual(self.pending()[0]["status"], "uncertain")

    def test_changed_native_identity_cannot_use_or_remint_credentials(self) -> None:
        token = self.mint()
        original = self.pin.copy()
        changes = {
            "thread_id": "replacement-thread",
            "start_ticks": int(str(self.pin["start_ticks"])) + 1,
            "host_start_ticks": int(str(self.pin["host_start_ticks"])) + 1,
            "boot_id": "another-boot",
            "argv_sha256": digest(b"another command"),
            "owner": "another-owner",
            "runner_id": "another-runner",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.pin = original | {field: value}
                self.assertEqual(self.client.post(self.root + "/events", headers=self.authorization(token), json=self.user_item()).status_code, 409)
                self.assertEqual(self.client.post(self.mint_path, headers=self.binding_headers).status_code, 409)
        self.pin = original
        self.assertEqual(self.calls, [])

    def test_malformed_bound_json_never_reaches_application(self) -> None:
        for body in (b'{"type":"message","type":"external_session_status","data":{"status":"idle"}}', b'{"type":', b"[]"):
            with self.subTest(body=body):
                headers = self.authorization() | {"Content-Type": "application/json"}
                self.assertGreaterEqual(self.client.post(self.root + "/events", headers=headers, content=body).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_unrelated_sessions_stay_usable_and_completion_mints_no_new_grant(self) -> None:
        self.assertEqual(self.client.post("/v1/sessions/unrelated/events", json={"type": "message", "role": "user", "content": "normal"}).status_code, 200)
        self.assertEqual(self.client.get(self.root).status_code, 409)
        self.advance("delivery_confirmed")
        self.assertFalse(self.mint().startswith(TOKEN_PREFIX))
        self.assertEqual(self.client.get(self.root).status_code, 200)
        self.assertEqual(self.client.post(f"/v1/sessions/{self.spec.expected.session_id}/events", json={"type": "message"}).status_code, 409)

    def test_installed_client_recovers_from_placeholder_and_posts_native_observations(self) -> None:
        self.exercise_installed_client(bootstrap=False)

    def test_installed_client_replaces_bootstrap_bearer_on_native_unauthorized(self) -> None:
        self.exercise_installed_client(bootstrap=True)

    def exercise_installed_client(self, *, bootstrap: bool) -> None:
        self.ready = False
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self.ingress.endpoint = ("127.0.0.1", listener.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(self.guard, log_level="error", access_log=False, lifespan="off"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()

        def stop_server() -> None:
            server.should_exit = True
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "ephemeral HTTP server did not stop")

        self.addCleanup(stop_server)
        deadline_s = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline_s:
            Event().wait(0.01)
        self.assertTrue(server.started)
        environment = {
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_RUNNER_DELEGATED_AUTH": "1",
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": self.binding_token,
            "OMNIGENT_CONFIG_HOME": str(self.directory / "config"),
            "OMNIGENT_DATA_DIR": str(self.directory / "data"),
            "DATABRICKS_CONFIG_FILE": str(self.directory / "absent_databricks_config"),
            "OMNIGENT_ANALYTICS": "0",
            "DISABLE_TELEMETRY": "true",
        }
        if bootstrap:
            environment["OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN"] = secrets.token_urlsafe(32)
        with patch.dict(os.environ, environment, clear=True), patch.object(_entry, "_runner_auth_factory", None):
            factory = _entry._make_auth_token_factory()
            self.assertIsNotNone(factory)
            _entry._set_runner_auth_factory(factory)

            async def exercise() -> None:
                async with open_server_client(base_url, auth=_entry._RunnerDatabricksAuth(factory), headers=self.binding_headers, timeout=httpx.Timeout(5)) as shared:
                    for suffix in ("", "/labels", "/agent/contents", "/items?limit=1&order=desc"):
                        response = await shared.get(self.root + suffix)
                        self.assertEqual(response.status_code, 200, response.text)
                self.ready = True
                self.assertIs(_entry._make_auth_token_factory(), factory)
                async with open_server_client(base_url, auth=_entry._RunnerDatabricksAuth(factory), timeout=httpx.Timeout(5)) as native:
                    response = await native.patch(self.root, json={"external_session_id": self.pin["thread_id"]})
                    self.assertEqual(response.status_code, 200, response.text)
                    await _post_mcp_startup(native, self.spec.new_session_id, {"fixture": {"status": "starting", "error": None}})
                    self.claim_manifest()
                    await _post_user_message(
                        native, self.spec.new_session_id, {"turnId": "turn-fixture"}, {"id": "item-fixture", "type": "userMessage", "content": [{"type": "text", "text": REQUEST.decode()}]}
                    )
                    await _post_status(native, self.spec.new_session_id, "running", response_id="turn-fixture")
                    await _post_agent_message(native, self.spec.new_session_id, {"turnId": "turn-fixture"}, {"type": "agentMessage", "text": "Observed response"})
                    await _post_output_text_delta(native, self.spec.new_session_id, "text", message_id="message-fixture", index=0, final=True)
                    await _post_output_reasoning_delta(native, self.spec.new_session_id, "reasoning", started=True)
                    await _post_tool_output_delta(native, self.spec.new_session_id, "output", call_id="call-fixture")
                    await _post_external_session_todos(native, session_id=self.spec.new_session_id, todos=[{"content": "fixture", "status": "completed", "activeForm": "fixture"}])
                    await _post_session_interrupted(native, self.spec.new_session_id, response_id="turn-fixture")
                    usage = _session_usage_data_from_params({"tokenUsage": {"total": {"inputTokens": 12, "outputTokens": 3, "cachedInputTokens": 4}, "modelContextWindow": 100}})
                    assert usage is not None
                    response = await _post_session_event(native, self.spec.new_session_id, event_type="external_session_usage", data={key: value for key, value in usage.items()})
                    assert response is not None
                    self.assertEqual(response.status_code, 200)

            asyncio.run(exercise())
        self.assertEqual(len(self.calls), 15)
        reads, writes = self.calls[:4], self.calls[4:]
        self.assertTrue(all(call["headers"].get(RUNNER_TUNNEL_TOKEN_HEADER.lower()) == self.binding_token for call in reads))
        self.assertTrue(all(RUNNER_TUNNEL_TOKEN_HEADER.lower() not in call["headers"] for call in writes))
        credentials = [call["headers"].get("authorization", "") for call in writes]
        self.assertTrue(all(value.startswith("Bearer " + TOKEN_PREFIX) for value in credentials))
        self.assertEqual(len(set(credentials)), len(writes))
        mcp, status, assistant = writes[1]["payload"], writes[3]["payload"], writes[4]["payload"]
        assert isinstance(mcp, dict) and isinstance(status, dict) and isinstance(assistant, dict)
        self.assertEqual(mcp["type"], "external_mcp_startup")
        self.assertEqual(writes[2]["payload"], self.user_item())
        self.assertEqual(status["type"], "external_session_status")
        self.assertEqual(assistant["data"]["item_data"]["agent"], "codex-native-ui")
        event_types = []
        for call in writes[5:]:
            payload = call["payload"]
            assert isinstance(payload, dict)
            event_types.append(payload["type"])
        self.assertEqual(
            event_types,
            ["external_output_text_delta", "external_output_reasoning_delta", "external_tool_output_delta", "external_session_todos", "external_session_interrupted", "external_session_usage"],
        )


if __name__ == "__main__":
    unittest.main()
