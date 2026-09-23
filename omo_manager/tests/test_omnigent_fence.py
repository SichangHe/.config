"""Exercise durable fencing with the installed Omnigent wire protocol."""

# pyright: reportUninitializedInstanceVariable=false

from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from threading import Barrier

from omnigent.host.frames import HostLaunchRunnerFrame, HostLaunchRunnerResultFrame, HostRunnerStatusFrame, decode_host_frame, encode_host_frame
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import PingFrame, PongFrame, RequestFrame, ResponseEndFrame, WSOpenFrame, decode_frame, encode_frame
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from omo_manager.omo_omnigent_fence import Admission, ExpectedState, FenceGuard, FenceStore, Operation, Rejected, ReplacementSpec, digest


AUTHORITY = b"authorized replacement of exactly the bound owner"
REQUEST = b"preserve the nine-item queue and deliver only open work"
EVIDENCE = digest(b"independently verified local fixture")
TOKEN = "old-runner-binding"


def expected_state() -> ExpectedState:
    return ExpectedState(
        session_id="old-session",
        runner_id=token_bound_runner_id(TOKEN),
        host_id="fixture-host",
        thread_id="old-thread",
        task_sha256=digest(b"task"),
        todo_sha256=digest(b"todo"),
        queue_sha256=digest(b"nine queued items"),
        workspace="/fixture/workspace",
        workspace_sha256=digest(b"workspace"),
        host_sha256=digest(b"host"),
        process_sha256=digest(b"process"),
        routing_sha256=digest(b"routing"),
        queue_count=9,
        routing_generation=0,
        host_generation=0,
    )


def phase_receipt(spec: ReplacementSpec, phase: str) -> dict[str, object]:
    receipt: dict[str, object] = {"operation_id": spec.operation_id, "evidence_sha256": EVIDENCE}
    if phase == "old_quiesced":
        receipt.update({key: getattr(spec.expected, key) for key in ("session_id", "runner_id", "thread_id", "process_sha256", "host_sha256")})
        receipt.update(native_writers_gone=True, pending_launches_absent=True, transport_drained=True)
    else:
        receipt["new_session_id"] = spec.new_session_id
    if phase == "delivery_confirmed":
        receipt["delivery_id"] = "delivery-once"
    return receipt


class OmnigentFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="omnigent-fence-test-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name).resolve() / "fence.sqlite"
        self.store = FenceStore(self.database)
        self.expected = expected_state()
        self.spec = ReplacementSpec("replace-owner", "new-session", digest(AUTHORITY), digest(REQUEST), self.expected)
        self.http_calls: list[str] = []

    def prepare(self) -> Operation:
        result = self.store.prepare(self.spec, self.expected, AUTHORITY, REQUEST)
        assert isinstance(result, Operation), result
        return result

    def fence(self) -> Operation:
        self.prepare()
        result = self.store.fence(self.spec.operation_id, self.expected)
        assert isinstance(result, Operation), result
        self.assertEqual(result.phase, "fenced")
        return result

    def snapshot(self) -> None:
        self.expected = replace(self.expected, routing_generation=self.store.generation(self.expected.session_id), host_generation=self.store.host_generation(self.expected.host_id))
        self.spec = replace(self.spec, expected=self.expected)

    def admit(self, kind: str = "http") -> Admission:
        result = self.store.admit([self.expected.session_id], kind)
        assert isinstance(result, Admission), result
        return result

    def test_idle_replacement_advances_once_and_old_owner_stays_fenced(self) -> None:
        operation = self.fence()
        for phase in ("old_quiesced", "new_prepared", "committed", "delivery_confirmed"):
            receipt = phase_receipt(self.spec, phase)
            if phase == "delivery_confirmed":
                self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
            operation = self.store.advance(self.spec.operation_id, phase, receipt)
            self.assertIsInstance(operation, Operation)
            self.assertEqual(self.store.advance(self.spec.operation_id, phase, receipt), operation)
            self.assertTrue(self.store.is_fenced(self.expected.session_id))
        assert isinstance(operation, Operation)
        self.assertEqual(operation.phase, "delivery_confirmed")
        self.assertIsInstance(self.store.admit([self.expected.session_id], "http"), Rejected)
        successor = self.store.admit([self.spec.new_session_id], "http")
        assert isinstance(successor, Admission)
        self.store.finish(successor)

    def test_every_changed_expected_field_rejects_without_reserving_operation(self) -> None:
        for field in fields(self.expected):
            value = getattr(self.expected, field.name)
            changed = value + 1 if isinstance(value, int) else value + "changed"
            with self.subTest(field=field.name):
                result = self.store.prepare(self.spec, replace(self.expected, **{field.name: changed}), AUTHORITY, REQUEST)
                self.assertIsInstance(result, Rejected)
                self.assertIsNone(self.store.get(self.spec.operation_id))

    def test_authority_and_request_bytes_must_match_without_persisting_operation(self) -> None:
        for authority, request in ((AUTHORITY + b"changed", REQUEST), (AUTHORITY, REQUEST + b"changed")):
            with self.subTest(authority=authority, request=request):
                self.assertIsInstance(self.store.prepare(self.spec, self.expected, authority, request), Rejected)
                self.assertIsNone(self.store.get(self.spec.operation_id))

    def test_duplicate_operation_is_idempotent_but_conflicting_payload_is_rejected(self) -> None:
        operation = self.prepare()
        self.assertEqual(self.prepare(), operation)
        changed_request = REQUEST + b"changed"
        changed = replace(self.spec, request_sha256=digest(changed_request))
        self.assertIsInstance(self.store.prepare(changed, self.expected, AUTHORITY, changed_request), Rejected)
        self.assertEqual(self.store.get(self.spec.operation_id), operation)

    def test_two_preparations_cannot_reserve_the_same_old_owner(self) -> None:
        self.prepare()
        competing = replace(self.spec, operation_id="competing-replacement", new_session_id="different-successor")
        self.assertIsInstance(self.store.prepare(competing, self.expected, AUTHORITY, REQUEST), Rejected)
        self.assertIsNone(self.store.get(competing.operation_id))

    def test_concurrent_preparations_reserve_exactly_one_successor(self) -> None:
        barrier = Barrier(2)

        def prepare_candidate(spec: ReplacementSpec) -> Operation | Rejected:
            store = FenceStore(self.database)
            barrier.wait(timeout=5)
            return store.prepare(spec, self.expected, AUTHORITY, REQUEST)

        competing = replace(self.spec, operation_id="competing-replacement", new_session_id="different-successor")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare_candidate, (self.spec, competing)))
        self.assertEqual(sum(isinstance(result, Operation) for result in results), 1)
        self.assertEqual(sum(isinstance(result, Rejected) for result in results), 1)
        self.assertEqual(sum(self.store.get(spec.operation_id) is not None for spec in (self.spec, competing)), 1)

    def test_prepared_cancellation_releases_reservation_for_a_fresh_operation(self) -> None:
        self.prepare()
        receipt = phase_receipt(self.spec, "cancelled") | {"successor_absent": True}
        cancelled = self.store.cancel_prepared(self.spec.operation_id, receipt)
        assert isinstance(cancelled, Operation)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.store.cancel_prepared(self.spec.operation_id, receipt), cancelled)
        self.assertIsInstance(self.store.prepare(self.spec, self.expected, AUTHORITY, REQUEST), Rejected)
        fresh = replace(self.spec, operation_id="fresh-replacement")
        self.assertIsInstance(self.store.prepare(fresh, self.expected, AUTHORITY, REQUEST), Operation)

    def test_fenced_cancellation_cannot_restore_old_owner(self) -> None:
        self.fence()
        receipt = phase_receipt(self.spec, "cancelled") | {"successor_absent": True}
        self.assertIsInstance(self.store.cancel_prepared(self.spec.operation_id, receipt), Rejected)
        self.assertTrue(FenceStore(self.database).is_fenced(self.expected.session_id))

    def test_reopen_preserves_every_phase_and_receipt(self) -> None:
        operation = self.prepare()
        for phase in ("prepared", "fenced", "old_quiesced", "new_prepared", "committed", "delivery_confirmed"):
            with self.subTest(phase=phase):
                if phase == "fenced":
                    operation = self.store.fence(self.spec.operation_id, self.expected)
                elif phase != "prepared":
                    if phase == "delivery_confirmed":
                        self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
                    operation = self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase))
                assert isinstance(operation, Operation), operation
                self.store = FenceStore(self.database)
                self.assertEqual(self.store.get(self.spec.operation_id), operation)
                self.assertEqual(self.store.is_fenced(self.expected.session_id), phase != "prepared")

    def test_uncertain_restart_requires_explicit_reconciliation_before_advancing(self) -> None:
        self.fence()
        self.store.mark_uncertain(self.spec.operation_id, "native shutdown result lost")
        self.store = FenceStore(self.database)
        receipt = phase_receipt(self.spec, "old_quiesced")
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", receipt), Rejected)
        unresolved = self.store.reconcile(self.spec.operation_id, receipt | {"resolved": False})
        assert isinstance(unresolved, Operation)
        self.assertEqual(unresolved.status, "reconciliation")
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", receipt), Rejected)
        self.assertIsInstance(self.store.reconcile(self.spec.operation_id, receipt | {"resolved": True}), Operation)
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", receipt), Operation)

    def test_completed_admission_invalidates_old_snapshot_at_prepare(self) -> None:
        admission = self.admit()
        self.store.finish(admission)
        self.assertGreater(self.store.generation(self.expected.session_id), self.expected.routing_generation)
        self.assertIsInstance(self.store.prepare(self.spec, self.expected, AUTHORITY, REQUEST), Rejected)
        self.assertIsNone(self.store.get(self.spec.operation_id))

    def test_completed_admission_between_prepare_and_fence_invalidates_snapshot(self) -> None:
        self.prepare()
        admission = self.admit()
        self.store.finish(admission)
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)
        self.assertFalse(self.store.is_fenced(self.expected.session_id))

    def test_concurrent_admission_and_fence_cannot_both_succeed(self) -> None:
        self.prepare()
        barrier = Barrier(2)

        def admit() -> Admission | Rejected:
            store = FenceStore(self.database)
            barrier.wait(timeout=5)
            return store.admit([self.expected.session_id], "http")

        def fence() -> Operation | Rejected:
            store = FenceStore(self.database)
            barrier.wait(timeout=5)
            return store.fence(self.spec.operation_id, self.expected)

        with ThreadPoolExecutor(max_workers=2) as pool:
            admission_future, fence_future = pool.submit(admit), pool.submit(fence)
            admission, operation = admission_future.result(), fence_future.result()
        if isinstance(admission, Admission):
            self.assertIsInstance(operation, Rejected)
            self.assertFalse(self.store.is_fenced(self.expected.session_id))
            self.store.finish(admission)
        else:
            self.assertIsInstance(operation, Operation)
            self.assertTrue(self.store.is_fenced(self.expected.session_id))

    def test_fence_rejects_outstanding_mutating_lease_even_with_fresh_snapshot(self) -> None:
        admission = self.admit("remote:request")
        self.snapshot()
        self.prepare()
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)
        self.assertFalse(self.store.is_fenced(self.expected.session_id))
        self.store.finish(admission)
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Operation)

    def test_completed_unrelated_host_launch_invalidates_prepare_snapshot(self) -> None:
        launch = self.store.admit_host_launch(self.expected.host_id, "unrelated-session", "remote:host.launch_runner")
        assert isinstance(launch, Admission)
        self.store.finish(launch)
        self.assertEqual(self.store.generation(self.expected.session_id), self.expected.routing_generation)
        self.assertGreater(self.store.host_generation(self.expected.host_id), self.expected.host_generation)
        self.assertIsInstance(self.store.prepare(self.spec, self.expected, AUTHORITY, REQUEST), Rejected)
        self.assertIsNone(self.store.get(self.spec.operation_id))

    def test_completed_host_launch_between_prepare_and_fence_invalidates_snapshot(self) -> None:
        self.prepare()
        launch = self.store.admit_host_launch(self.expected.host_id, "unrelated-session", "remote:host.launch_runner")
        assert isinstance(launch, Admission)
        self.store.finish(launch)
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)
        self.assertFalse(self.store.is_fenced(self.expected.session_id))

    def test_host_launch_must_drain_even_when_snapshot_includes_its_generation(self) -> None:
        launch = self.store.admit_host_launch(self.expected.host_id, "unrelated-session", "remote:host.launch_runner")
        assert isinstance(launch, Admission)
        self.snapshot()
        self.prepare()
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)
        self.store.finish(launch)
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Operation)

    def test_host_launch_exclusivity_allows_only_reserved_successor_after_quiescence(self) -> None:
        self.fence()
        for session_id in (self.expected.session_id, self.spec.new_session_id, "unrelated-session"):
            with self.subTest(phase="fenced", session=session_id):
                self.assertIsInstance(self.store.admit_host_launch(self.expected.host_id, session_id, "remote:host.launch_runner"), Rejected)
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", phase_receipt(self.spec, "old_quiesced")), Operation)
        self.assertIsInstance(self.store.admit_host_launch(self.expected.host_id, "unrelated-session", "remote:host.launch_runner"), Rejected)
        successor = self.store.admit_host_launch(self.expected.host_id, self.spec.new_session_id, "remote:host.launch_runner")
        assert isinstance(successor, Admission), successor
        self.store.finish(successor)
        other_host = self.store.admit_host_launch("other-host", "unrelated-session", "remote:host.launch_runner")
        assert isinstance(other_host, Admission), other_host
        self.store.finish(other_host)
        for phase in ("new_prepared", "committed"):
            self.assertIsInstance(self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase)), Operation)
            self.assertIsInstance(self.store.admit_host_launch(self.expected.host_id, "unrelated-session", "remote:host.launch_runner"), Rejected)
        self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "delivery_confirmed", phase_receipt(self.spec, "delivery_confirmed")), Operation)
        released = self.store.admit_host_launch(self.expected.host_id, "unrelated-session", "remote:host.launch_runner")
        assert isinstance(released, Admission), released
        self.store.finish(released)

    def test_delivery_intent_survives_restart_and_cannot_be_claimed_twice(self) -> None:
        self.fence()
        for phase in ("old_quiesced", "new_prepared", "committed"):
            self.assertIsInstance(self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase)), Operation)
        claimed = self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST))
        assert isinstance(claimed, Operation), claimed
        self.store = FenceStore(self.database)
        self.assertEqual(self.store.get(self.spec.operation_id), claimed)
        self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Rejected)
        receipt = phase_receipt(self.spec, "delivery_confirmed")
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "delivery_confirmed", receipt | {"delivery_id": "different-delivery"}), Rejected)
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "delivery_confirmed", receipt), Operation)

    def test_lifetime_lease_allows_fence_but_quiescence_waits_for_closure(self) -> None:
        admission = self.admit("websocket")
        self.fence()
        receipt = phase_receipt(self.spec, "old_quiesced")
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", receipt), Rejected)
        self.store.finish(admission)
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", receipt), Operation)

    def test_process_crash_preserves_admission_until_dead_holder_reconciliation(self) -> None:
        source = """
import os
import sys
from pathlib import Path
from omo_manager.omo_omnigent_fence import Admission, FenceStore
store = FenceStore(Path(sys.argv[1]))
admission = store.admit(["old-session"], "http")
assert isinstance(admission, Admission)
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", source, str(self.database)], check=True, timeout=10, capture_output=True)
        self.store = FenceStore(self.database)
        admissions = self.store.active_admissions(self.expected.session_id)
        self.assertEqual(len(admissions), 1)
        self.snapshot()
        self.prepare()
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)
        self.assertIsNone(self.store.reconcile_admission(admissions[0], EVIDENCE))
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Operation)

    def test_live_holder_cannot_be_reconciled_away(self) -> None:
        admission = self.admit()
        self.assertIsInstance(self.store.reconcile_admission(admission, EVIDENCE), Rejected)
        self.assertIn(admission, self.store.active_admissions(self.expected.session_id))
        self.store.finish(admission)

    def client(self, websocket_endpoint: Callable[[WebSocket], Awaitable[None]] | None = None) -> TestClient:
        async def http_endpoint(request: Request) -> JSONResponse:
            self.http_calls.append(request.url.path)
            return JSONResponse({"path": request.url.path, "body": (await request.body()).decode()})

        async def websocket_default(websocket: WebSocket) -> None:
            await websocket.accept()
            while (await websocket.receive())["type"] != "websocket.disconnect":
                pass

        app = Starlette(routes=[Route("/{path:path}", http_endpoint, methods=["GET", "POST", "PATCH", "DELETE"]), WebSocketRoute("/{path:path}", websocket_endpoint or websocket_default)])
        return TestClient(FenceGuard(app, self.store))

    def test_http_blocks_old_owner_across_body_and_mutating_routes(self) -> None:
        self.fence()
        calls = (
            ("POST", "/v1/sessions", {"session_id": self.expected.session_id}),
            ("POST", f"/v1/sessions/{self.expected.session_id}/events", {"type": "message", "content": "work"}),
            ("POST", f"/v1/sessions/{self.expected.session_id}/events", {"type": "retry_session"}),
            ("PATCH", f"/v1/sessions/{self.expected.session_id}/codex_goal/status", {"status": "active"}),
            ("PATCH", f"/api/conversations/{self.expected.session_id}", {"title": "new title"}),
            ("POST", "/api/conversations", {"parent_session_id": self.expected.session_id}),
        )
        with self.client() as client:
            for method, path, payload in calls:
                with self.subTest(method=method, path=path):
                    self.assertEqual(client.request(method, path, json=payload).status_code, 409)
                    self.assertEqual(self.http_calls, [])
            self.assertEqual(client.post("/v1/sessions", json={"host_id": "other-host", "session_id": "unrelated-session"}).status_code, 200)
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.post("/api/unrelated", content=b"opaque").status_code, 200)

    def test_http_admitted_before_prepare_cannot_reuse_stale_expected_state(self) -> None:
        with self.client() as client:
            response = client.post(f"/v1/sessions/{self.expected.session_id}/events", json={"type": "message", "content": "work"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.store.active_admissions(self.expected.session_id))
        self.assertIsInstance(self.store.prepare(self.spec, self.expected, AUTHORITY, REQUEST), Rejected)

    def test_public_http_cannot_claim_reserved_successor_before_delivery(self) -> None:
        self.fence()
        with self.client() as client:
            self.assertEqual(client.post("/v1/sessions", json={"session_id": "unrelated-session"}).status_code, 409)
            for path in ("/v1/sessions", f"/api/hosts/{self.expected.host_id}/runners"):
                with self.subTest(path=path):
                    self.assertEqual(client.post(path, json={"host_id": self.expected.host_id, "session_id": self.spec.new_session_id}).status_code, 409)
            self.assertEqual(self.http_calls, [])
            self.assertEqual(client.post("/v1/sessions", json={"host_id": "other-host", "session_id": "unrelated-session"}).status_code, 200)
            allowed_calls = list(self.http_calls)
            self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", phase_receipt(self.spec, "old_quiesced")), Operation)
            self.assertEqual(client.post("/v1/sessions", json={"host_id": self.expected.host_id, "session_id": self.spec.new_session_id}).status_code, 409)
            self.assertEqual(client.post("/v1/sessions", json={"host_id": self.expected.host_id, "session_id": "unrelated-session"}).status_code, 409)
            for phase in ("new_prepared", "committed"):
                self.assertIsInstance(self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase)), Operation)
                self.assertEqual(client.post(f"/v1/sessions/{self.spec.new_session_id}/events", json={"type": "message", "content": "work"}).status_code, 409)
            self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
            self.assertEqual(client.post(f"/v1/sessions/{self.spec.new_session_id}/events", json={"type": "message", "content": "work"}).status_code, 409)
            self.assertEqual(self.http_calls, allowed_calls)
            self.assertIsInstance(self.store.advance(self.spec.operation_id, "delivery_confirmed", phase_receipt(self.spec, "delivery_confirmed")), Operation)
            self.assertEqual(client.post(f"/v1/sessions/{self.spec.new_session_id}/events", json={"type": "message", "content": "work"}).status_code, 200)

    def test_ambiguous_http_json_is_rejected_without_calling_application(self) -> None:
        self.fence()
        with self.client() as client:
            for body in ('{"session_id":"old-session","session_id":"new-session"}', '{"session_id":'):
                with self.subTest(body=body):
                    response = client.post("/v1/sessions", content=body, headers={"content-type": "application/json"})
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.http_calls, [])

    def test_old_stored_history_is_readable_but_refresh_and_reserved_successor_are_denied(self) -> None:
        self.fence()
        generation = self.store.generation(self.expected.session_id)
        with self.client() as client:
            self.assertEqual(client.get(f"/v1/sessions/{self.expected.session_id}/items").status_code, 200)
            self.assertEqual(client.get(f"/v1/sessions/{self.expected.session_id}/items?refresh=true").status_code, 409)
            self.assertEqual(client.get(f"/v1/sessions/{self.spec.new_session_id}/items").status_code, 409)
        self.assertEqual(self.store.generation(self.expected.session_id), generation)

    def test_retired_runner_reconnect_and_terminal_attach_are_denied(self) -> None:
        self.fence()
        paths = (
            f"/api/runners/{self.expected.runner_id}/tunnel",
            f"/v1/sessions/{self.expected.session_id}/resources/terminals/terminal_bash_s1/attach",
        )
        with self.client() as client:
            for path in paths:
                with self.subTest(path=path), self.assertRaises(WebSocketDisconnect) as caught:
                    with client.websocket_connect(path):
                        self.fail("retired connection accepted")
                self.assertEqual(caught.exception.code, 1008)

    def test_reserved_successor_terminal_is_denied_until_delivery_confirmation(self) -> None:
        self.prepare()
        path = f"/v1/sessions/{self.spec.new_session_id}/resources/terminals/terminal_bash_s1/attach"
        with self.client() as client:
            for phase in ("prepared", "fenced", "old_quiesced", "new_prepared", "committed", "delivery_intent"):
                if phase == "fenced":
                    self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Operation)
                elif phase == "delivery_intent":
                    self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
                elif phase != "prepared":
                    self.assertIsInstance(self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase)), Operation)
                with self.subTest(phase=phase), self.assertRaises(WebSocketDisconnect) as caught:
                    with client.websocket_connect(path):
                        self.fail("reserved successor terminal accepted")
                self.assertEqual(caught.exception.code, 1008)
            self.assertIsInstance(self.store.advance(self.spec.operation_id, "delivery_confirmed", phase_receipt(self.spec, "delivery_confirmed")), Operation)
            with client.websocket_connect(path):
                pass

    def test_idle_existing_terminal_and_runner_connections_close_after_fence(self) -> None:
        paths = (
            f"/api/runners/{self.expected.runner_id}/tunnel",
            f"/v1/sessions/{self.expected.session_id}/resources/terminals/terminal_bash_s1/attach",
        )
        self.prepare()
        with self.client() as client:
            with client.websocket_connect(paths[0]) as runner, client.websocket_connect(paths[1]) as terminal:
                self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Operation)
                for websocket in (runner, terminal):
                    self.assertEqual(websocket.receive()["type"], "websocket.close")
        self.assertFalse(self.store.active_admissions(self.expected.session_id))

    def test_actual_outgoing_init_frames_cannot_target_retired_session(self) -> None:
        self.fence()
        for encoding in ("utf-8", "base64"):
            body = json.dumps(
                {
                    "session_id": self.expected.session_id,
                    "agent_id": "fixture-agent",
                    "sub_agent_name": None,
                    "session_init": {
                        "protocol_version": 2,
                        "server_version": "test",
                        "session_id": self.expected.session_id,
                        "agent_id": "fixture-agent",
                        "sub_agent_name": None,
                        "snapshot": {"workspace": self.expected.workspace, "harness_override": "codex-native"},
                        "suppress_recovery_turn": False,
                    },
                }
            )
            if encoding == "base64":
                body = base64.b64encode(body.encode()).decode()
            frame = encode_frame(RequestFrame("init-request", "POST", "/v1/sessions", body=body, encoding=encoding))

            async def endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.send_text(frame)
                await websocket.receive()

            with self.subTest(encoding=encoding), self.client(endpoint) as client:
                with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                    self.assertEqual(websocket.receive()["type"], "websocket.close")

    def test_actual_terminal_open_frame_cannot_target_retired_session(self) -> None:
        self.fence()
        frame = encode_frame(WSOpenFrame("terminal-channel", f"/v1/sessions/{self.expected.session_id}/resources/terminals/terminal_bash_s1/attach"))

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(frame)
            await websocket.receive()

        with self.client(endpoint) as client:
            with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                self.assertEqual(websocket.receive()["type"], "websocket.close")

    def test_real_response_end_releases_remote_request_admission(self) -> None:
        frame = encode_frame(RequestFrame("init-request", "POST", "/v1/sessions", body=json.dumps({"session_id": self.expected.session_id})))

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(frame)
            await websocket.receive_text()
            await websocket.send_text(encode_frame(PongFrame(1)))
            await websocket.receive()

        with self.client(endpoint) as client:
            with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                self.assertIsInstance(decode_frame(websocket.receive_text()), RequestFrame)
                self.assertTrue(any(item.kind == "remote:request" for item in self.store.active_admissions(self.expected.session_id)))
                websocket.send_text(encode_frame(ResponseEndFrame("init-request")))
                self.assertIsInstance(decode_frame(websocket.receive_text()), PongFrame)
                self.assertFalse(any(item.kind == "remote:request" for item in self.store.active_admissions(self.expected.session_id)))
        self.snapshot()
        self.fence()

    def test_manifest_claim_requires_exact_native_binding_and_survives_reopen(self) -> None:
        self.fence()
        for phase in ("old_quiesced", "new_prepared", "committed"):
            self.assertIsInstance(self.store.advance(self.spec.operation_id, phase, phase_receipt(self.spec, phase)), Operation)
        runner = token_bound_runner_id("new-runner-binding")
        self.assertIsInstance(self.store.claim_successor_launch(self.expected.host_id, self.spec.new_session_id, runner, "launch-once"), Operation)
        self.assertIsInstance(self.store.claim_delivery(self.spec.operation_id, "delivery-once", digest(REQUEST)), Operation)
        self.assertIsInstance(self.store.successor_dispatch_conflict(self.spec.new_session_id, initialization=False, runner_id=runner), Rejected)
        self.assertIsNone(self.store.successor_dispatch_conflict(self.spec.new_session_id, initialization=True, runner_id=runner))
        self.assertIsInstance(self.store.claim_manifest_dispatch(self.spec.operation_id, "delivery-once", digest(REQUEST), runner, "new-thread"), Rejected)
        native: dict[str, object] = {"operation_id": self.spec.operation_id, "evidence_sha256": EVIDENCE, "thread_id": "new-thread"}
        self.assertIsInstance(self.store.record_evidence(self.spec.operation_id, "native_successor_verified", native), Operation)
        for delivery, prompt, claimed_runner, thread in (
            ("wrong", digest(REQUEST), runner, "new-thread"),
            ("delivery-once", digest(b"other"), runner, "new-thread"),
            ("delivery-once", digest(REQUEST), "wrong", "new-thread"),
            ("delivery-once", digest(REQUEST), runner, "wrong"),
        ):
            self.assertIsInstance(self.store.claim_manifest_dispatch(self.spec.operation_id, delivery, prompt, claimed_runner, thread), Rejected)
        self.assertIsInstance(self.store.claim_manifest_dispatch(self.spec.operation_id, "delivery-once", digest(REQUEST), runner, "new-thread"), Operation)
        self.assertIsNone(self.store.successor_dispatch_conflict(self.spec.new_session_id, initialization=False, runner_id=runner))
        reopened = FenceStore(self.database)
        self.assertIsInstance(reopened.claim_manifest_dispatch(self.spec.operation_id, "delivery-once", digest(REQUEST), runner, "new-thread"), Rejected)

    def test_disconnect_does_not_erase_unacknowledged_remote_work(self) -> None:
        frame = encode_frame(RequestFrame("init-request", "POST", "/v1/sessions", body=json.dumps({"session_id": self.expected.session_id})))

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(frame)
            await websocket.receive()

        with self.client(endpoint) as client:
            with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                websocket.receive_text()
        self.assertTrue(any(item.kind == "remote:request" for item in FenceStore(self.database).active_admissions(self.expected.session_id)))
        self.snapshot()
        self.prepare()
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)

    def test_retired_host_launch_returns_local_failure_and_host_still_launches_successor(self) -> None:
        self.fence()
        received: list[object] = []
        retired = HostLaunchRunnerFrame("retired-launch", TOKEN, self.expected.workspace, self.expected.session_id, "codex-native")
        successor = HostLaunchRunnerFrame("successor-launch", "successor-binding", self.expected.workspace, self.spec.new_session_id, "codex-native")
        probe = HostRunnerStatusFrame("status-probe", token_bound_runner_id(successor.binding_token))

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(encode_host_frame(retired))
            received.append(decode_host_frame(await websocket.receive_text()))
            result = self.store.advance(self.spec.operation_id, "old_quiesced", phase_receipt(self.spec, "old_quiesced"))
            assert isinstance(result, Operation), result
            await websocket.send_text(encode_host_frame(successor))
            received.append(decode_host_frame(await websocket.receive_text()))
            await websocket.send_text(encode_host_frame(probe))
            await websocket.receive()

        with self.client(endpoint) as client:
            with client.websocket_connect("/api/hosts/fixture-host/tunnel") as websocket:
                outgoing = decode_host_frame(websocket.receive_text())
                self.assertEqual(outgoing, successor)
                local_failure = received[0]
                assert isinstance(local_failure, HostLaunchRunnerResultFrame)
                self.assertEqual(local_failure.request_id, retired.request_id)
                self.assertEqual(local_failure.error_code, "session_fenced")
                result = HostLaunchRunnerResultFrame(successor.request_id, "launched", token_bound_runner_id(successor.binding_token))
                websocket.send_text(encode_host_frame(result))
                self.assertEqual(decode_host_frame(websocket.receive_text()), probe)
                self.assertEqual(received[1], result)
        self.assertFalse(self.store.active_admissions(self.spec.new_session_id))

    def test_host_tunnel_admits_runner_keepalive_ping_pong(self) -> None:
        ping = encode_frame(PingFrame(1))
        pong = encode_frame(PongFrame(1))
        probe = HostRunnerStatusFrame("still-connected", "unrelated-runner")
        received: list[str] = []

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(ping)
            received.append(await websocket.receive_text())
            await websocket.send_text(encode_host_frame(probe))
            await websocket.receive()

        with self.client(endpoint) as client:
            with client.websocket_connect("/api/hosts/fixture-host/tunnel") as websocket:
                self.assertEqual(websocket.receive_text(), ping)
                websocket.send_text(pong)
                self.assertEqual(decode_host_frame(websocket.receive_text()), probe)
        self.assertEqual(received, [pong])
        with self.client() as client:
            with client.websocket_connect("/api/hosts/fixture-host/tunnel") as websocket:
                websocket.send_text(encode_frame(RequestFrame("host-request", "GET", "/v1/sessions")))
                self.assertEqual(websocket.receive()["type"], "websocket.close")

    def test_malformed_and_unbound_incoming_tunnel_frames_close_connection(self) -> None:
        frames = (
            "{not-json",
            '{"kind":"ping","kind":"pong","ts":1}',
            '{"kind":"unknown"}',
            '{"kind":"request","id":"missing-target"}',
            encode_frame(ResponseEndFrame("unissued-request")),
        )
        with self.client() as client:
            for frame in frames:
                with self.subTest(frame=frame):
                    with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                        websocket.send_text(frame)
                        self.assertEqual(websocket.receive()["type"], "websocket.close")
            with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                websocket.send_bytes(encode_frame(PingFrame(1)).encode())
                self.assertEqual(websocket.receive()["type"], "websocket.close")

    def test_malformed_outgoing_target_bodies_never_reach_runner(self) -> None:
        frames = (
            RequestFrame("bad-json", "POST", "/v1/sessions", body='{"session_id":'),
            RequestFrame("duplicate-target", "POST", "/v1/sessions", body='{"session_id":"old-session","session_id":"new-session"}'),
            RequestFrame("bad-base64", "POST", "/v1/sessions", body="***", encoding="base64"),
            RequestFrame("bad-encoding", "POST", "/v1/sessions", body='{"session_id":"old-session"}', encoding="unknown"),
        )
        for frame in frames:

            async def endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.send_text(encode_frame(frame))
                await websocket.receive()

            with self.subTest(frame=frame.id), self.client(endpoint) as client:
                with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                    self.assertEqual(websocket.receive()["type"], "websocket.close")

    def test_retired_query_and_nested_initialization_id_never_reach_runner(self) -> None:
        self.fence()
        frames = (
            RequestFrame("query-request", "POST", "/v1/elicitations/example", query_string="session_id=old-session"),
            WSOpenFrame("query-channel", "/v1/example", query_string="session_id=old-session"),
            RequestFrame("nested-init", "POST", "/v1/sessions", body=json.dumps({"session_id": "unrelated", "session_init": {"session_id": self.expected.session_id}})),
        )
        for frame in frames:

            async def endpoint(websocket: WebSocket) -> None:
                await websocket.accept()
                await websocket.send_text(encode_frame(frame))
                await websocket.receive()

            with self.subTest(frame=frame), self.client(endpoint) as client:
                with client.websocket_connect("/api/runners/unrelated-runner/tunnel") as websocket:
                    self.assertEqual(websocket.receive()["type"], "websocket.close")

    def test_canonical_uuid_aliases_cannot_bypass_session_or_host_fence(self) -> None:
        host_alias = "01234567-89ab-cdef-0123-456789abcdef"
        session_alias = "11234567-89ab-cdef-0123-456789abcdef"
        self.expected = replace(self.expected, host_id=host_alias.replace("-", ""), session_id=session_alias.replace("-", ""))
        self.spec = replace(self.spec, expected=self.expected)
        self.fence()
        received: list[object] = []
        launch = HostLaunchRunnerFrame("alias-launch", "unrelated-binding", self.expected.workspace, "unrelated-session", "codex-native")
        probe = HostRunnerStatusFrame("still-connected", "unrelated-runner")

        async def endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            await websocket.send_text(encode_host_frame(launch))
            received.append(decode_host_frame(await websocket.receive_text()))
            await websocket.send_text(encode_host_frame(probe))
            await websocket.receive()

        with self.client(endpoint) as client:
            self.assertEqual(client.post(f"/v1/sessions/{session_alias}/events", json={"type": "message"}).status_code, 409)
            self.assertEqual(client.post("/v1/sessions", json={"session_id": session_alias}).status_code, 409)
            self.assertEqual(client.post("/v1/sessions", json={"host_id": host_alias}).status_code, 409)
            with client.websocket_connect(f"/api/hosts/{host_alias}/tunnel") as websocket:
                self.assertEqual(decode_host_frame(websocket.receive_text()), probe)
        self.assertIsInstance(received[0], HostLaunchRunnerResultFrame)
        assert isinstance(received[0], HostLaunchRunnerResultFrame)
        self.assertEqual(received[0].status, "failed")

    def test_single_successor_runner_claim_and_store_provenance_survive_reopen(self) -> None:
        self.fence()
        self.assertIsInstance(self.store.advance(self.spec.operation_id, "old_quiesced", phase_receipt(self.spec, "old_quiesced")), Operation)
        self.assertIsInstance(self.store.mark_uncertain(self.spec.operation_id, "initial create started; inspect on crash"), Operation)
        claimed_runner = token_bound_runner_id("successor-binding")
        claimed = self.store.claim_successor_launch(self.expected.host_id, self.spec.new_session_id, claimed_runner, "one-launch")
        self.assertIsInstance(claimed, Operation)
        reopened = FenceStore(self.database)
        self.assertEqual(reopened.session_for_runner(claimed_runner), self.spec.new_session_id)
        self.assertIsInstance(reopened.claim_successor_launch(self.expected.host_id, self.spec.new_session_id, claimed_runner, "one-launch"), Rejected)
        self.assertIsInstance(reopened.successor_dispatch_conflict(self.spec.new_session_id, initialization=True, runner_id="wrong-runner"), Rejected)
        self.assertIsNone(reopened.successor_dispatch_conflict(self.spec.new_session_id, initialization=True, runner_id=claimed_runner))
        self.assertIsInstance(reopened.admit([self.spec.new_session_id], "store:set_labels"), Rejected)
        with reopened.successor_context("wrong-operation"):
            self.assertIsInstance(reopened.admit([self.spec.new_session_id], "store:set_labels"), Rejected)
        with reopened.successor_context(self.spec.operation_id):
            admission = reopened.admit([self.spec.new_session_id], "store:set_labels")
            self.assertIsInstance(admission, Admission)
            assert isinstance(admission, Admission)
            reopened.finish(admission)
            self.assertIsInstance(reopened.admit([self.expected.session_id], "store:set_labels"), Rejected)

    def test_hyphenated_snapshot_uses_the_same_admission_and_host_identity(self) -> None:
        session_id = "11234567-89ab-cdef-0123-456789abcdef"
        host_id = "01234567-89ab-cdef-0123-456789abcdef"
        self.expected = replace(self.expected, session_id=session_id, host_id=host_id)
        self.spec = replace(self.spec, expected=self.expected)
        admission = self.store.admit([session_id.replace("-", "")], "http")
        assert isinstance(admission, Admission)
        self.snapshot()
        self.prepare()
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Rejected)
        self.store.finish(admission)
        self.assertIsInstance(self.store.fence(self.spec.operation_id, self.expected), Operation)
        self.assertTrue(self.store.is_fenced(session_id.replace("-", "")))
        self.assertTrue(self.store.is_fenced(session_id))
        self.assertIsInstance(self.store.admit_host_launch(host_id.replace("-", ""), "unrelated", "remote:host.launch_runner"), Rejected)
        saved = self.store.for_old_session(session_id.replace("-", ""))
        assert isinstance(saved, Operation)
        self.assertEqual(saved.spec.expected.session_id, session_id)
        self.assertEqual(saved.spec.expected.host_id, host_id)

    def test_prepare_persists_exact_raw_packet_in_the_same_reservation(self) -> None:
        request = b"exact packet\r\nwith original bytes\r\n"
        self.spec = replace(self.spec, request_sha256=digest(request))
        prepared = self.store.prepare(self.spec, self.expected, AUTHORITY, request)
        self.assertIsInstance(prepared, Operation)
        reopened = FenceStore(self.database).for_old_session(self.expected.session_id)
        assert isinstance(reopened, Operation)
        packet = reopened.receipts["exact_packet"]
        self.assertEqual(base64.b64decode(str(packet["packet_b64"])), request)
        self.assertEqual(packet["evidence_sha256"], digest(request))


if __name__ == "__main__":
    unittest.main()
