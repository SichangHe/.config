"""Exercise the reviewed OmniGent package against isolated SQLite databases."""
# pyright: reportUninitializedInstanceVariable=false
from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import secrets
import sqlite3
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from uuid import uuid4

from omo_manager import omo_omnigent_server as server

HAS_OMNIGENT = importlib.util.find_spec("omnigent") is not None
if TYPE_CHECKING or HAS_OMNIGENT:
    import httpx
    from sqlalchemy import event
    from starlette.requests import HTTPConnection

    from omnigent import runtime
    from omnigent.entities import Conversation, NewConversationItem
    from omnigent.entities.conversation import FunctionCallData, MessageData
    from omnigent.runner.identity import token_bound_runner_id
    from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider, create_auth_provider
    from omnigent.session_import.models import (
        IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
        IMPORT_SOURCE_LABEL_KEY,
    )
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    from omo_manager.omo_omnigent_fence import ExpectedState, FenceStore, Operation, Rejected, ReplacementSpec, _boot_id, _process_start, digest
    from omo_manager.tests.test_omnigent_fence import phase_receipt
    from omo_manager.omo_omnigent_replace import ReplacementBackend


def required[T](value: T | None) -> T:
    assert value is not None
    return value


# 🧑 "Replace the current read-only owner atomically with one unrestricted OmniGent session ... preserving the task and queue and delivering only open work."
@unittest.skipUnless(HAS_OMNIGENT, "requires the reviewed OmniGent installation")
class OmniGentServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "server.json"
        self.config.write_text(json.dumps({"providers": {}}), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("OMNIGENT_")}
        environment.update({
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_CONFIG": str(self.config),
            "OMNIGENT_CONFIG_HOME": str(self.root / "config"),
            "OMNIGENT_DATA_DIR": str(self.root / "data"),
            "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(self.root / "state" / "admin-credentials"),
            "DO_NOT_TRACK": "1",
        })
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.options = server.ServerOptions(
            database_uri=f"sqlite:///{self.root / 'app.sqlite'}",
            artifact_location=str(self.root / "artifacts"),
            fence_database=self.root / "fence.sqlite",
            config_path=self.config,
        )

    def store(self) -> tuple[SqlAlchemyConversationStore, SqlAlchemyConversationStore, FenceStore]:
        raw = SqlAlchemyConversationStore(self.options.database_uri)
        self.addCleanup(raw._engine.dispose)
        fence = FenceStore(self.options.fence_database)
        return raw, cast(SqlAlchemyConversationStore, cast(object, server.GuardedConversationStore(raw, fence))), fence

    def prepare(self, fence: FenceStore, conversation: Conversation, new_session_id: str | None = None) -> ReplacementSpec:
        expected = ExpectedState(
            session_id=conversation.id,
            runner_id=required(conversation.runner_id),
            host_id=conversation.host_id or "host-old",
            thread_id=conversation.external_session_id or "thread-old",
            task_sha256=digest(b"task"),
            todo_sha256=digest(b"todo"),
            queue_sha256=digest(b"queue"),
            workspace=str(self.root),
            workspace_sha256=digest(b"workspace"),
            host_sha256=digest(b"host"),
            process_sha256=digest(b"process"),
            routing_sha256=digest(b"routing"),
            queue_count=9,
            routing_generation=fence.generation(conversation.id),
            host_generation=fence.host_generation(conversation.host_id or "host-old"),
        )
        spec = ReplacementSpec(str(uuid4()), new_session_id or str(uuid4()), digest(b"authority"), digest(b"request"), expected)
        self.assertIsInstance(fence.prepare(spec, expected, b"authority", b"request"), Operation)
        return spec

    def retire(self, fence: FenceStore, conversation: Conversation) -> ReplacementSpec:
        spec = self.prepare(fence, conversation)
        self.assertIsInstance(fence.fence(spec.operation_id, spec.expected), Operation)
        return spec

    def message(self, text: str) -> NewConversationItem:
        return NewConversationItem(
            type="message",
            response_id=str(uuid4()),
            data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
        )

    def test_package_is_bound_to_installed_version_and_exact_source(self) -> None:
        self.assertEqual(importlib.metadata.version("omnigent"), server.OMNIGENT_VERSION)
        self.assertEqual(server.validate_package(), server.OMNIGENT_SOURCE_SHA256)
        with patch.object(server, "OMNIGENT_VERSION", "unsupported-test-version"):
            result = server.validate_package()
            self.assertIsInstance(result, server.BootstrapRejected)
            assert isinstance(result, server.BootstrapRejected)
            self.assertEqual(result.code, "unsupported_version")
        with patch.object(server, "OMNIGENT_SOURCE_SHA256", "0" * 64):
            result = server.validate_package()
            self.assertIsInstance(result, server.BootstrapRejected)
            assert isinstance(result, server.BootstrapRejected)
            self.assertEqual(result.code, "unsupported_source")

    def test_unsupported_bootstraps_do_not_create_storage(self) -> None:
        cases = [
            (replace(self.options, host="0.0.0.0"), {}, {}, "unsupported_bind"),
            (replace(self.options, host="localhost"), {}, {}, "unsupported_bind"),
            (replace(self.options, host="::1"), {}, {}, "unsupported_bind"),
            (replace(self.options, port=True), {}, {}, "unsupported_bind"),
            (self.options, {}, {"OMNIGENT_AUTH_PROVIDER": "accounts"}, "unsupported_auth"),
            (replace(self.options, database_uri="sqlite:///:memory:"), {}, {}, "unsupported_storage"),
            (replace(self.options, database_uri="postgresql://localhost/test"), {}, {}, "unsupported_storage"),
            (replace(self.options, artifact_location="relative-artifacts"), {}, {}, "unsupported_storage"),
            (replace(self.options, artifact_location=str(self.options.fence_database)), {}, {}, "invalid_storage"),
            (replace(self.options, artifact_location=str(self.config)), {}, {}, "invalid_storage"),
            (replace(self.options, execution_timeout_s=0), {}, {}, "invalid_timeout"),
            (self.options, {"execution_timeout": False}, {}, "invalid_timeout"),
            (replace(self.options, database_uri=f"sqlite:///{self.options.fence_database}"), {}, {}, "invalid_storage"),
            (self.options, {"unexpected": True}, {}, "unsupported_config"),
            (self.options, {"routing": {"provider": "external"}}, {}, "unsupported_routing"),
            (self.options, {"providers": {"gateway": {"kind": "databricks"}}}, {}, "unsupported_routing"),
            (self.options, {"policies": {"broken": {"type": "unknown"}}}, {}, "invalid_policies"),
            (replace(self.options, fence_database=self.root / "missing" / "fence.sqlite", database_uri=f"sqlite:///{self.root / 'never-created' / 'app.sqlite'}"), {}, {}, "unsafe_fence_path"),
        ]
        for options, config, environment, code in cases:
            with self.subTest(code=code, config=config), patch.dict(os.environ, environment):
                self.config.write_text(json.dumps({"providers": {}} | config), encoding="utf-8")
                before = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
                for factory in (server.validate_options, server.create_server):
                    result = factory(options)
                    self.assertIsInstance(result, server.BootstrapRejected)
                    assert isinstance(result, server.BootstrapRejected)
                    self.assertEqual(result.code, code)
                self.assertEqual(before, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))

    def test_auth_without_local_identity_is_rejected_before_storage_creation(self) -> None:
        cases = [
            ("header", None, None), ("header", "0", None), ("header", "false", None), (None, "0", None), (None, "false", None),
            ("header", "1", "Authorization"), ("header", "1", "X-Omnigent-Runner-Tunnel-Token"), ("header", "1", "Content-Type"),
        ]
        for source, local_flag, auth_header in cases:
            with self.subTest(source=source, local_flag=local_flag, auth_header=auth_header), patch.dict(os.environ):
                for name, value in (("OMNIGENT_AUTH_PROVIDER", source), ("OMNIGENT_LOCAL_SINGLE_USER", local_flag), ("OMNIGENT_AUTH_HEADER", auth_header)):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                provider = create_auth_provider()
                self.assertIsInstance(provider, UnifiedAuthProvider)
                native_headers = [(b"authorization", b"Bearer omo_native_fixture"), (b"x-omnigent-runner-tunnel-token", b"fixture-binding"), (b"content-type", b"application/json")]
                self.assertNotEqual(provider.get_user_id(HTTPConnection({"type": "http", "headers": native_headers})), "local")
                before_paths = sorted(path.relative_to(self.root) for path in self.root.rglob("*"))
                before_environment = dict(os.environ)
                for factory in (server.validate_options, server.create_server):
                    result = factory(self.options)
                    assert isinstance(result, server.BootstrapRejected), result
                    self.assertEqual(result.code, "unsupported_auth")
                self.assertEqual(before_paths, sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
                self.assertEqual(before_environment, dict(os.environ))

    def test_default_single_user_auth_and_explicit_cli_storage_precedence(self) -> None:
        self.config.write_text(json.dumps({
            "database_uri": f"sqlite:///{self.root / 'ignored.sqlite'}",
            "artifact_location": str(self.root / "ignored-artifacts"),
        }), encoding="utf-8")
        with patch.dict(os.environ), patch.dict(vars(runtime._globals)):
            del os.environ["OMNIGENT_AUTH_PROVIDER"]
            del os.environ["OMNIGENT_LOCAL_SINGLE_USER"]
            application = server.create_server(self.options)
            self.assertIsInstance(application, server.ServerApplication)
            assert isinstance(application, server.ServerApplication)
            self.addCleanup(cast(SqlAlchemyConversationStore, application.conversation_store._store)._engine.dispose)
            self.assertEqual(application.plan.options.database_uri, self.options.database_uri)
            self.assertEqual(application.plan.options.artifact_location, self.options.artifact_location)
            self.assertFalse((self.root / "ignored.sqlite").exists())
            self.assertFalse((self.root / "ignored-artifacts").exists())

            async def exercise() -> None:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url="http://testserver") as client:
                    info = (await client.get("/v1/info")).json()
                    self.assertTrue(info["single_user"])
                    self.assertFalse(info["accounts_enabled"])
                    self.assertFalse(info["smart_routing_enabled"])
                    self.assertEqual((await client.get("/v1/me")).json()["user_id"], "local")
                    self.assertIsNone((await client.get("/v1/me", headers={"X-Forwarded-Email": "local"})).json()["user_id"])

            asyncio.run(exercise())

    def test_private_control_socket_accepts_only_digest_bound_coordinator_envelopes(self) -> None:
        preparation = self.root / "preparation.json"
        backend_config = self.root / "backend.json"
        for path in (preparation, backend_config):
            path.write_bytes(b"{}")
            path.chmod(0o600)
        options = server.ReplacementControlOptions(
            self.root / "control.sock", preparation, self.root / "packet.json", self.root / "review.txt", backend_config, digest(b"{}"),
        )
        with patch.dict(vars(runtime._globals)):
            application = server.create_server(self.options)
            self.assertIsInstance(application, server.ServerApplication)
            assert isinstance(application, server.ServerApplication)
            self.addCleanup(cast(SqlAlchemyConversationStore, application.conversation_store._store)._engine.dispose)
            backend = cast(ReplacementBackend, cast(object, None))
            control = server.ReplacementControl(options, application, backend)

            async def request(payload: bytes) -> dict[str, object]:
                reader, writer = await asyncio.open_unix_connection(options.socket_path)
                try:
                    writer.write(payload + b"\n")
                    await writer.drain()
                    return json.loads(await reader.readline())
                finally:
                    writer.close()
                    await writer.wait_closed()

            async def exercise() -> None:
                self.assertIsNone(await control.start())
                try:
                    self.assertEqual(stat.S_IMODE(options.socket_path.stat().st_mode), 0o600)
                    envelope = {"action": "inspect", "preparation_sha256": "", "packet_sha256": "a" * 64, "approval_sha256": ""}
                    def encode(value: Mapping[str, object]) -> bytes:
                        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                    missing = await request(encode(envelope))
                    self.assertEqual(missing["code"], "control_failed")
                    self.assertFalse(options.packet_path.exists())
                    for payload in (encode(envelope | {"action": "quiesce"}), encode(envelope | {"observed": True}), encode(envelope)[:-1] + b',"action":"execute"}'):
                        with self.subTest(payload=payload):
                            self.assertEqual((await request(payload))["code"], "invalid_control_request")
                    other = server.ReplacementControl(options, application, backend)
                    self.assertEqual(required(await other.start()).code, "control_already_running")
                    preparation.write_bytes(b'{"changed":true}')
                    self.assertEqual((await request(encode(envelope)))["code"], "invalid_control_binding")
                finally:
                    await control.close()
                self.assertFalse(options.socket_path.exists())

            asyncio.run(exercise())

    def test_control_configuration_rejects_unowned_shapes_before_store_creation(self) -> None:
        preparation = self.root / "preparation.json"
        backend_config = self.root / "backend.json"
        for path in (preparation, backend_config):
            path.write_bytes(b"{}")
            path.chmod(0o600)
        options = server.ReplacementControlOptions(self.root / "control.sock", preparation, self.root / "packet.json", self.root / "review.txt", backend_config, digest(b"{}"))
        self.assertIsNone(server.validate_control(options))
        self.assertEqual(required(server.validate_control(replace(options, socket_path=self.root / ("x" * 110)))).code, "invalid_control_path")
        preparation.chmod(0o644)
        before = sorted(self.root.rglob("*"))
        result = server.create_server(replace(self.options, replacement_control=options))
        self.assertIsInstance(result, server.BootstrapRejected)
        assert isinstance(result, server.BootstrapRejected)
        self.assertEqual(result.code, "invalid_control_path")
        self.assertEqual(before, sorted(self.root.rglob("*")))
        preparation.chmod(0o600)
        result = server.create_server(replace(self.options, replacement_control=options))
        self.assertIsInstance(result, server.BootstrapRejected)
        assert isinstance(result, server.BootstrapRejected)
        self.assertEqual(result.code, "invalid_backend_config")
        self.assertEqual(before, sorted(self.root.rglob("*")))

    def test_fenced_mutations_cannot_change_history_or_attach_old_runner(self) -> None:
        raw, guarded, fence = self.store()
        old = guarded.create_conversation(title="original", runner_id="runner-old", host_id=uuid4().hex, workspace=str(self.root))
        other = guarded.create_conversation(title="unrelated", runner_id="runner-other")
        guarded.append(old.id, [self.message("preserved history")])
        self.assertEqual(fence.active_admissions(old.id), ())
        self.retire(fence, old)
        before = raw.get_conversation(old.id)
        attempts = [
            ("append", (old.id, [self.message("forbidden")]), {}),
            ("update_conversation", (old.id,), {"title": "forbidden"}),
            ("set_labels", (old.id, {"forbidden": "true"}), {}),
            ("set_host_id", (old.id, "host-other"), {"workspace": str(self.root)}),
            ("set_external_session_id", (old.id, "thread-other"), {}),
            ("fork_conversation", (old.id,), {}),
            ("create_conversation", (), {"parent_conversation_id": old.id}),
            ("create_conversation", (), {"runner_id": "runner-old"}),
            ("create_conversation", (), {"host_id": old.host_id, "workspace": str(self.root)}),
            ("set_runner_id", (other.id, "runner-old"), {}),
            ("replace_runner_id", (other.id, "runner-old"), {}),
            ("create_session_with_agent", (), {
                "agent_id": str(uuid4()), "agent_name": "forbidden", "agent_bundle_location": "missing-bundle",
                "agent_description": None, "runner_id": "runner-old",
            }),
        ]
        for name, args, kwargs in attempts:
            with self.subTest(method=name, kwargs=kwargs), self.assertRaisesRegex(RuntimeError, "session fenced"):
                getattr(guarded, name)(*args, **kwargs)
        self.assertEqual(before, raw.get_conversation(old.id))
        self.assertEqual(len(raw.list_conversations().data), 2)
        self.assertEqual(len(raw.list_items(old.id).data), 1)
        self.assertEqual(fence.active_admissions(old.id), ())
        guarded.set_labels(other.id, {"independent": "yes"})
        guarded.append(other.id, [self.message("independent history")])
        self.assertEqual(required(raw.get_conversation(other.id)).labels["independent"], "yes")
        fresh = guarded.create_conversation(host_id=uuid4().hex, workspace=str(self.root))
        self.assertIsNotNone(raw.get_conversation(fresh.id))

    def test_factory_preserves_auth_runtime_and_split_storage_through_asgi(self) -> None:
        self.config.write_text(json.dumps({
            "providers": {}, "routing": {"provider": "none"}, "execution_timeout": 123,
            "admins": ["admin@example.test"],
            "policies": {"admin__audit": {"type": "function", "function": "test_policy.audit"}},
        }), encoding="utf-8")
        options = replace(self.options, conversation_database_uri=f"sqlite:///{self.root / 'conversations.sqlite'}")
        with patch.dict(vars(runtime._globals)):
            application = server.create_server(options)
            self.assertIsInstance(application, server.ServerApplication)
            assert isinstance(application, server.ServerApplication)
            self.addCleanup(cast(SqlAlchemyConversationStore, application.conversation_store._store)._engine.dispose)
            self.addCleanup(cast(SqlAlchemyConversationStore, application.conversation_store._store)._conv_engine.dispose)
            self.assertIs(runtime.get_conversation_store(), application.conversation_store)
            self.assertEqual(runtime.get_caps().execution_timeout, 123)
            self.assertEqual([policy.name for policy in runtime.get_caps().default_policies], ["admin__audit"])
            artifacts = required(runtime.get_artifact_store())
            self.assertIsNotNone(artifacts)
            artifacts.put("fixture.txt", b"isolated artifact")
            self.assertEqual(artifacts.get("fixture.txt"), b"isolated artifact")
            self.assertEqual((self.root / "artifacts" / "fixture.txt").read_bytes(), b"isolated artifact")
            guarded = cast(SqlAlchemyConversationStore, cast(object, application.conversation_store))
            created = guarded.create_session_with_agent(
                agent_id=uuid4().hex, agent_name="test session", agent_bundle_location="missing-test-bundle",
                agent_description=None,
            )
            session_id = created.conversation.id
            permission_store = SqlAlchemyPermissionStore(options.database_uri)
            permission_store.grant("owner@example.test", session_id, LEVEL_OWNER)
            independent = guarded.create_session_with_agent(
                agent_id=uuid4().hex, agent_name="independent", agent_bundle_location="missing-test-bundle",
                agent_description=None,
            ).conversation
            permission_store.grant("owner@example.test", independent.id, LEVEL_OWNER)
            guarded.append(session_id, [self.message("history remains visible")])
            guarded.set_runner_id(session_id, "runner-old")
            old = required(guarded.get_conversation(session_id))
            self.retire(application.fence_store, old)

            async def exercise() -> None:
                transport = httpx.ASGITransport(app=application.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    admin = await client.get("/v1/me", headers={"X-Forwarded-Email": "admin@example.test"})
                    self.assertEqual(admin.status_code, 200)
                    self.assertEqual(admin.json(), {"user_id": "admin@example.test", "is_admin": True})
                    user = await client.get("/v1/me", headers={"X-Forwarded-Email": "owner@example.test"})
                    self.assertEqual(user.json(), {"user_id": "owner@example.test", "is_admin": False})
                    anonymous = await client.get("/v1/policies", headers={"X-Forwarded-Email": "local"})
                    self.assertEqual(anonymous.status_code, 401)
                    headers = {"X-Forwarded-Email": "owner@example.test"}
                    policies = await client.get("/v1/policies", headers=headers)
                    self.assertEqual(policies.status_code, 200)
                    self.assertEqual(policies.json()["data"][0]["source"], "config")
                    self.assertEqual(policies.json()["data"][0]["handler"], "test_policy.audit")
                    history = await client.get(f"/v1/sessions/{session_id}/items", headers=headers)
                    self.assertEqual(history.status_code, 200, history.text)
                    self.assertEqual(len(history.json()["data"]), 1)
                    private_history = await client.get(f"/v1/sessions/{session_id}/items", headers={"X-Forwarded-Email": "local"})
                    self.assertEqual(private_history.status_code, 401)
                    refreshing = await client.get(f"/v1/sessions/{session_id}/items?refresh=true", headers=headers)
                    self.assertEqual(refreshing.status_code, 409)
                    denied = await client.patch(f"/v1/sessions/{session_id}", json={"title": "forbidden"}, headers=headers)
                    self.assertEqual(denied.status_code, 409, denied.text)
                    updated = await client.patch(f"/v1/sessions/{independent.id}", json={"title": "still usable"}, headers=headers)
                    self.assertEqual(updated.status_code, 200, updated.text)
                    self.assertEqual(updated.json()["title"], "still usable")

            asyncio.run(exercise())
            with closing(sqlite3.connect(self.root / "app.sqlite")) as metadata:
                self.assertEqual(metadata.execute("SELECT count(*) FROM agents").fetchone()[0], 2)
            with closing(sqlite3.connect(self.root / "conversations.sqlite")) as conversations:
                self.assertEqual(conversations.execute("SELECT count(*) FROM conversations").fetchone()[0], 2)
                self.assertEqual(conversations.execute("SELECT count(*) FROM conversation_items").fetchone()[0], 1)
            self.assertEqual(application.fence_store.active_admissions(session_id), ())

    def test_native_callbacks_write_reserved_successor_without_dispatching_controls(self) -> None:
        """The resolver pins this test process; auth, routes and SQLite stay real."""
        with patch.dict(os.environ, {"OMNIGENT_LOCAL_SINGLE_USER": "1"}), patch.dict(vars(runtime._globals)):
            application = server.create_server(self.options)
            self.assertIsInstance(application, server.ServerApplication)
            assert isinstance(application, server.ServerApplication)
            guarded, fence = cast(SqlAlchemyConversationStore, cast(object, application.conversation_store)), application.fence_store
            raw = cast(SqlAlchemyConversationStore, application.conversation_store._store)
            self.addCleanup(raw._engine.dispose)
            binding_token = secrets.token_urlsafe(32)
            runner_id = token_bound_runner_id(binding_token)
            host_id = uuid4().hex
            old = guarded.create_conversation(runner_id="runner-old", host_id=host_id, workspace=str(self.root))
            successor = guarded.create_session_with_agent(
                agent_id=uuid4().hex, agent_name="native fixture", agent_bundle_location="missing-test-bundle",
                agent_description=None, title="Native fixture", runner_id=runner_id,
            ).conversation
            raw.set_host_id(successor.id, host_id, workspace=str(self.root))
            SqlAlchemyPermissionStore(self.options.database_uri).grant("local", successor.id, LEVEL_OWNER)
            spec = self.prepare(fence, old, successor.id)
            self.assertIsInstance(fence.fence(spec.operation_id, spec.expected), Operation)
            self.assertIsInstance(fence.advance(spec.operation_id, "old_quiesced", phase_receipt(spec, "old_quiesced")), Operation)
            self.assertIsInstance(fence.claim_successor_launch(host_id, successor.id, runner_id, "launch-once"), Operation)
            for phase in ("new_prepared", "committed"):
                self.assertIsInstance(fence.advance(spec.operation_id, phase, phase_receipt(spec, phase)), Operation)
            prompt = "preserve the nine-item queue and deliver only open work"
            self.assertIsInstance(fence.claim_delivery(spec.operation_id, "delivery-once", digest(prompt.encode())), Operation)
            thread_id = "native-fixture-thread"
            native_pin: dict[str, object] = {
                "session_id": successor.id, "runner_id": runner_id, "owner": "local", "thread_id": thread_id,
                "pid": os.getpid(), "start_ticks": _process_start(os.getpid()), "boot_id": _boot_id(),
                "argv_sha256": digest(Path(f"/proc/{os.getpid()}/cmdline").read_bytes()),
                "host_pid": os.getpid(), "host_start_ticks": _process_start(os.getpid()), "host_id": host_id,
            }
            self.assertIsNotNone(application.native_ingress)
            required(application.native_ingress).bind_native_resolver(lambda _: native_pin.copy())
            session_path = f"/v1/sessions/{successor.id}"
            item_data: dict[str, object] = {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
            manifest_data = {"item_type": "message", "item_data": item_data, "response_id": "codex_turn-fixture"}
            manifest = {"type": "external_conversation_item", "data": manifest_data}

            async def exercise() -> None:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url=f"http://127.0.0.1:{self.options.port}") as client:
                    async def credentials() -> dict[str, str]:
                        minted = await client.post(f"/v1/runners/{runner_id}/token", headers={"X-Omnigent-Runner-Tunnel-Token": binding_token})
                        self.assertEqual(minted.status_code, 200, minted.text)
                        self.assertTrue(minted.json()["token"].startswith("omo_native_"))
                        return {"Authorization": f"Bearer {minted.json()['token']}"}

                    for host, port in (("192.0.2.1", self.options.port), ("127.0.0.1", self.options.port + 1)):
                        with self.subTest(scope_host=host, scope_port=port):
                            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url=f"http://{host}:{port}") as foreign_endpoint:
                                denied_mint = await foreign_endpoint.post(
                                    f"/v1/runners/{runner_id}/token",
                                    headers={"X-Omnigent-Runner-Tunnel-Token": binding_token, "Host": f"127.0.0.1:{self.options.port}"},
                                )
                                self.assertEqual(denied_mint.status_code, 409, denied_mint.text)
                                self.assertEqual(denied_mint.json()["error"], "native_endpoint_mismatch")
                    before_metadata = raw.get_conversation(successor.id)
                    foreign_metadata = await client.patch(
                        session_path, json={"external_session_id": native_pin["thread_id"]},
                        headers=(await credentials()) | {"X-Forwarded-Email": "foreign@example.test"},
                    )
                    self.assertEqual(foreign_metadata.status_code, 401, foreign_metadata.text)
                    self.assertEqual(raw.get_conversation(successor.id), before_metadata)
                    metadata = await client.patch(session_path, json={"external_session_id": native_pin["thread_id"]}, headers=await credentials())
                    self.assertEqual(metadata.status_code, 200, metadata.text)
                    self.assertEqual(required(raw.get_conversation(successor.id)).external_session_id, native_pin["thread_id"])
                    premature = await client.post(session_path + "/events", json=manifest, headers=await credentials())
                    self.assertEqual(premature.status_code, 409, premature.text)
                    self.assertEqual(raw.list_items(successor.id).data, [])
                    evidence: dict[str, object] = {"operation_id": spec.operation_id, "evidence_sha256": digest(b"verified fixture process"), "thread_id": thread_id}
                    self.assertIsInstance(fence.record_evidence(spec.operation_id, "native_successor_verified", evidence), Operation)
                    self.assertIsInstance(fence.claim_manifest_dispatch(spec.operation_id, "delivery-once", digest(prompt.encode()), runner_id, thread_id), Operation)
                    mirrored = await client.post(session_path + "/events", json=manifest, headers=await credentials())
                    self.assertEqual(mirrored.status_code, 202, mirrored.text)
                    self.assertFalse(mirrored.json()["queued"])
                    items = raw.list_items(successor.id).data
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0].id, mirrored.json()["item_id"])
                    assert isinstance(items[0].data, MessageData)
                    self.assertEqual(items[0].data.content, item_data["content"])
                    snapshot = raw.get_conversation(successor.id)
                    forbidden = [
                        ("POST", "/events", {"type": "message", "data": item_data}),
                        ("POST", "/events", {"type": "retry_session", "data": {}}),
                        ("POST", "/events", {"type": "stop_session", "data": {}}),
                        ("PATCH", "", {"model_override": "forbidden"}),
                        ("PATCH", "", {"external_session_id": native_pin["thread_id"], "title": "forbidden"}),
                        ("POST", "/events", manifest | {"data": manifest_data | {"type": "message"}}),
                        ("POST", "/events", manifest | {"data": manifest_data | {"item_data": item_data | {"data": {"type": "retry_session"}}}}),
                        ("POST", "/events", manifest | {"data": manifest_data | {"item_data": {"role": "user", "content": [{"type": "input_text", "text": "different user input"}]}}}),
                    ]
                    for method, suffix, payload in forbidden:
                        with self.subTest(method=method, payload=payload):
                            denied = await client.request(method, session_path + suffix, json=payload, headers=await credentials())
                            self.assertEqual(denied.status_code, 409, denied.text)
                            self.assertIn(denied.json()["error"], {"native_callback_scope_denied", "native_manifest_mismatch"})
                            self.assertEqual(raw.get_conversation(successor.id), snapshot)
                            self.assertEqual(raw.list_items(successor.id).data, items)
                    unauthenticated = await client.post(session_path + "/events", json={"type": "message", "data": item_data})
                    self.assertEqual(unauthenticated.status_code, 409, unauthenticated.text)
                    self.assertEqual(unauthenticated.json()["error"], "successor_reserved")
                    replay = await client.post(session_path + "/events", json=manifest, headers=await credentials())
                    self.assertEqual(replay.status_code, 409, replay.text)
                    self.assertEqual(replay.json()["error"], "native_callback_replayed")
                    self.assertEqual(raw.list_items(successor.id).data, items)
                    tool_data = {"agent": "codex-native-ui", "call_id": "call-fixture", "name": "exec_command", "arguments": '{"cmd":"true"}'}
                    tool_mirror = {"type": "external_conversation_item", "data": {"item_type": "function_call", "item_data": tool_data, "response_id": "codex_turn-fixture"}}
                    foreign_tool = await client.post(
                        session_path + "/events", json=tool_mirror,
                        headers=(await credentials()) | {"X-Forwarded-Email": "foreign@example.test"},
                    )
                    self.assertEqual(foreign_tool.status_code, 409, foreign_tool.text)
                    self.assertEqual(foreign_tool.json()["error"], "native_deferred_stock_auth_denied")
                    self.assertEqual(raw.get_conversation(successor.id), snapshot)
                    self.assertEqual(raw.list_items(successor.id).data, items)
                    with closing(sqlite3.connect(fence.path)) as pending:
                        self.assertEqual(pending.execute("SELECT count(*) FROM ingress_pending").fetchone(), (0,))
                    tool_headers = await credentials()
                    tool_scope = {"type": "http", "headers": [(key.lower().encode(), value.encode()) for key, value in tool_headers.items()]}
                    self.assertEqual(required(application.native_ingress).resolve_user(tool_scope), "local")
                    deferred = asyncio.create_task(client.post(session_path + "/events", json=tool_mirror, headers=tool_headers))
                    try:
                        async with asyncio.timeout(3):
                            while not any(admission.kind == "native:waiting" for admission in fence.active_admissions(successor.id)):
                                if deferred.done():
                                    self.fail(f"tool callback was not deferred: {(await deferred).text}")
                                await asyncio.sleep(0.01)
                        self.assertFalse(deferred.done())
                        self.assertEqual(raw.list_items(successor.id).data, items)
                        with closing(sqlite3.connect(fence.path)) as pending:
                            self.assertEqual(pending.execute("SELECT status FROM ingress_pending").fetchall(), [("pending",)])
                        self.assertIsInstance(fence.advance(spec.operation_id, "delivery_confirmed", phase_receipt(spec, "delivery_confirmed")), Operation)
                        released = await asyncio.wait_for(deferred, timeout=3)
                    finally:
                        if not deferred.done():
                            deferred.cancel()
                            await asyncio.gather(deferred, return_exceptions=True)
                    self.assertEqual(released.status_code, 202, released.text)
                    self.assertFalse(released.json()["queued"])
                    self.assertEqual(required(application.native_ingress).resolve_user(tool_scope), "local")
                    persisted = raw.list_items(successor.id).data
                    self.assertEqual(len(persisted), 2)
                    self.assertEqual(persisted[-1].id, released.json()["item_id"])
                    assert isinstance(persisted[-1].data, FunctionCallData)
                    self.assertEqual(persisted[-1].data.model_dump(), tool_data)
                    with closing(sqlite3.connect(fence.path)) as pending:
                        self.assertEqual(pending.execute("SELECT status FROM ingress_pending").fetchall(), [("released",)])
                    tool_replay = await client.post(session_path + "/events", json=tool_mirror)
                    self.assertEqual(tool_replay.status_code, 409, tool_replay.text)
                    self.assertEqual(tool_replay.json()["error"], "native_callback_replayed")
                    self.assertEqual(raw.list_items(successor.id).data, persisted)

            asyncio.run(exercise())
            self.assertEqual(fence.active_admissions(successor.id), ())
            final_operation = fence.get(spec.operation_id)
            assert isinstance(final_operation, Operation)
            self.assertEqual(final_operation.phase, "delivery_confirmed")

    def test_replacement_binding_read_is_narrow_and_does_not_unmask_public_reads(self) -> None:
        raw, guarded, fence = self.store()
        old = guarded.create_conversation(runner_id="runner-old", host_id=uuid4().hex, workspace=str(self.root))
        old = guarded.set_external_session_id(old.id, "thread-old")
        spec = self.retire(fence, old)
        adapter = cast(server.GuardedConversationStore, cast(object, guarded))
        self.assertEqual(adapter.replacement_session(spec.operation_id), old)
        self.assertIsNone(required(guarded.get_conversation(old.id)).runner_id)
        missing = adapter.replacement_session("unknown-operation")
        self.assertIsInstance(missing, server.BootstrapRejected)
        assert isinstance(missing, server.BootstrapRejected)
        self.assertEqual(missing.code, "unknown_operation")
        raw.replace_runner_id(old.id, "changed-outside-adapter")
        changed = adapter.replacement_session(spec.operation_id)
        self.assertIsInstance(changed, server.BootstrapRejected)
        assert isinstance(changed, server.BootstrapRejected)
        self.assertEqual(changed.code, "binding_changed")

    def test_mutation_after_prepare_invalidates_expected_state(self) -> None:
        raw, guarded, fence = self.store()
        old = guarded.create_conversation(runner_id="runner-old")
        spec = self.prepare(fence, old)
        guarded.update_conversation(old.id, title="changed while prepared")
        result = fence.fence(spec.operation_id, spec.expected)
        self.assertIsInstance(result, Rejected)
        assert isinstance(result, Rejected)
        self.assertEqual(result.code, "expected_state_mismatch")
        self.assertFalse(fence.is_fenced(old.id))
        self.assertEqual(required(raw.get_conversation(old.id)).title, "changed while prepared")

    def test_history_remains_readable_without_runner_connectivity(self) -> None:
        raw, guarded, fence = self.store()
        old = guarded.create_conversation(title="historical", runner_id="runner-old")
        other = guarded.create_conversation(title="independent", runner_id="runner-other")
        guarded.set_labels(old.id, {IMPORT_SOURCE_LABEL_KEY: "codex", IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY: "imported-thread"})
        items = guarded.append(old.id, [self.message("historicalneedle")])
        guarded.touch_runner_liveness(["runner-old", "runner-other"], 100)
        self.retire(fence, old)
        self.assertIsNone(required(guarded.get_conversation(old.id)).runner_id)
        self.assertIsNone(guarded.get_conversations([old.id])[old.id].runner_id)
        self.assertIsNone(required(guarded.find_imported_conversation("codex", "imported-thread")).runner_id)
        listed = {conversation.id: conversation for conversation in guarded.list_conversations().data}
        self.assertEqual(listed[old.id].title, "historical")
        self.assertIsNone(listed[old.id].runner_id)
        self.assertEqual(listed[other.id].runner_id, "runner-other")
        self.assertEqual(guarded.list_items(old.id).data, items)
        self.assertEqual(guarded.search("historicalneedle", conversation_id=old.id), items)
        self.assertEqual(guarded.get_runner_ids([old.id, other.id]), {old.id: None, other.id: "runner-other"})
        self.assertEqual(guarded.list_conversations_by_runner_id("runner-old"), [])
        self.assertEqual([conversation.id for conversation in guarded.list_conversations_by_runner_id("runner-other")], [other.id])
        connectivity = guarded.get_session_connectivity([old.id, other.id])
        self.assertIsNone(connectivity[old.id].runner_id)
        self.assertIsNone(connectivity[old.id].runner_last_seen)
        self.assertEqual(connectivity[other.id].runner_last_seen, 100)
        guarded.touch_runner_liveness(["runner-old", "runner-other"], 200)
        guarded.clear_runner_liveness("runner-old")
        direct = raw.get_session_connectivity([old.id, other.id])
        self.assertEqual(direct[old.id].runner_last_seen, 100)
        self.assertEqual(direct[other.id].runner_last_seen, 200)
        guarded.clear_runner_liveness("runner-other")
        self.assertIsNone(raw.get_session_connectivity([other.id])[other.id].runner_last_seen)

    def test_sql_write_keeps_admission_until_commit_and_blocks_fence(self) -> None:
        raw, guarded, fence = self.store()
        old = guarded.create_conversation(runner_id="runner-old")
        write_attempted = threading.Event()

        def observe_write(connection, cursor, statement, parameters, context, executemany) -> None:
            if statement.lower().startswith("update conversations"):
                write_attempted.set()

        event.listen(raw._engine, "before_cursor_execute", observe_write)
        self.addCleanup(event.remove, raw._engine, "before_cursor_execute", observe_write)
        blocker = sqlite3.connect(self.root / "app.sqlite")
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(guarded.append, old.id, [self.message("admitted before fence")])
            try:
                self.assertTrue(write_attempted.wait(timeout=3), "real SQL write was not reached")
                self.assertFalse(pending.done())
                admissions = fence.active_admissions(old.id)
                self.assertEqual(len(admissions), 1)
                self.assertEqual(admissions[0].kind, "store:append")
                spec = self.prepare(fence, old)
                result = fence.fence(spec.operation_id, spec.expected)
                self.assertIsInstance(result, Rejected)
                assert isinstance(result, Rejected)
                self.assertEqual(result.code, "admissions_outstanding")
                self.assertFalse(fence.is_fenced(old.id))
            finally:
                blocker.rollback()
            written = pending.result(timeout=3)
        self.assertEqual(raw.list_items(old.id).data, written)
        self.assertEqual(fence.active_admissions(old.id), ())
        self.assertIsInstance(fence.fence(spec.operation_id, spec.expected), Operation)
        with self.assertRaisesRegex(RuntimeError, "session fenced"):
            guarded.append(old.id, [self.message("arrived after fence")])

    def test_async_delete_cannot_remove_fenced_descendants(self) -> None:
        raw, guarded, fence = self.store()
        parent = guarded.create_conversation(title="parent")
        child = guarded.create_conversation(parent_conversation_id=parent.id, runner_id="runner-old")
        other = guarded.create_conversation(title="independent")
        self.retire(fence, child)
        for session_id in (child.id, parent.id):
            with self.subTest(session_id=session_id), self.assertRaisesRegex(RuntimeError, "session fenced"):
                asyncio.run(guarded.delete_conversation(session_id))
        self.assertIsNotNone(raw.get_conversation(parent.id))
        self.assertIsNotNone(raw.get_conversation(child.id))
        self.assertTrue(asyncio.run(guarded.delete_conversation(other.id)))
        self.assertIsNone(raw.get_conversation(other.id))
        self.assertEqual(fence.active_admissions(other.id), ())


if __name__ == "__main__":
    unittest.main()
