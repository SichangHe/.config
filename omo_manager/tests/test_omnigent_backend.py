"""Real Linux process barriers, native protocol and reserved-store recovery."""

from __future__ import annotations

import json
import io
import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from websockets.asyncio.server import Server

from omo_manager.omo_omnigent_backend import (
    Process,
    BackendConfig,
    BackendResources,
    OmnigentBackend,
    old_processes_gone,
    pidfd_signal,
    process,
    process_domain,
    read_config,
    stop_domain,
    host_standby,
    local_server_port,
    verify_host_socket,
    verify_empty_native_thread,
)
from omo_manager.omo_omnigent_fence import FenceStore, Rejected, ReplacementSpec, canonical, digest
from omo_manager.omo_omnigent_replace import ExactPacket, FileImage, ProcessPin, RuntimeState, WorkspacePin, decoded, encoded

OLD = "a" * 32
NEW = "b" * 32
AGENT = "c" * 32
HOST = "d" * 32


def rejected(value: object) -> Rejected:
    assert isinstance(value, Rejected), value
    return value


def present[T](value: T | None) -> T:
    assert value is not None
    return value


def running(pid: int, domain: str) -> Process:
    value = process(pid, domain)
    assert isinstance(value, Process), value
    return value


class ProcessBarrierTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.children: list[subprocess.Popen[bytes]] = []

    def setUp(self) -> None:
        self.addCleanup(self.reap)

    def reap(self) -> None:
        for child in reversed(self.children):
            if child.poll() is None:
                child.send_signal(signal.SIGCONT)
                child.kill()
            child.wait(timeout=3)
            if child.stdin:
                child.stdin.close()
            if child.stdout:
                child.stdout.close()

    def child(self, *arguments: str) -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *arguments], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(child)
        return child

    def pins(self, pid: int) -> tuple[ProcessPin, ...]:
        domain = process_domain(pid, ())
        assert not isinstance(domain, Rejected), domain
        return tuple(value.pin for value in domain)

    def test_real_pinned_process_stops_and_exit_is_positive(self) -> None:
        child = self.child()
        pins = self.pins(child.pid)
        self.assertIsNone(stop_domain(pins, child.pid, ()))
        self.assertIsNone(old_processes_gone(pins))
        self.assertEqual(child.wait(timeout=3), -signal.SIGKILL)

    def test_start_tick_mismatch_has_no_signal(self) -> None:
        child = self.child()
        pins = self.pins(child.pid)
        wrong = (replace(pins[0], start_ticks=pins[0].start_ticks + 1),)
        with patch("omo_manager.omo_omnigent_backend.pidfd_signal", wraps=pidfd_signal) as signals:
            result = stop_domain(wrong, child.pid, ())
        self.assertIsInstance(result, Rejected)
        self.assertEqual(rejected(result).code, "process_drift")
        self.assertEqual(signals.call_count, 0)
        self.assertIsNone(child.poll())

    def test_external_matching_writer_is_not_adopted_or_stopped(self) -> None:
        marker = uuid.uuid4().hex
        child = self.child()
        outsider = self.child(marker)
        result = process_domain(child.pid, (marker,))
        self.assertIsInstance(result, Rejected)
        self.assertEqual(rejected(result).code, "external_native_writer")
        self.assertIsNone(outsider.poll())

    def test_existing_pause_is_preserved(self) -> None:
        child = self.child()
        pins = self.pins(child.pid)
        child.send_signal(signal.SIGSTOP)
        for _ in range(100):
            current = process(child.pid, "host")
            if isinstance(current, Process) and current.state == "T":
                break
            time.sleep(0.01)
        result = stop_domain(pins, child.pid, ())
        self.assertIsInstance(result, Rejected)
        self.assertEqual(rejected(result).code, "process_already_stopped")
        self.assertEqual(running(child.pid, "host").state, "T")

    def test_freeze_failure_resumes_only_owned_unpaused_process(self) -> None:
        child = self.child()
        pins = self.pins(child.pid)
        actual_signal = pidfd_signal
        count = 0

        def fail_census(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                return Rejected("process_drift", "concurrent fork")
            return process_domain(*args, **kwargs)

        with patch("omo_manager.omo_omnigent_backend.process_domain", side_effect=fail_census), patch("omo_manager.omo_omnigent_backend.pidfd_signal", wraps=actual_signal) as signals:
            result = stop_domain(pins, child.pid, ())
        self.assertIsInstance(result, Rejected)
        self.assertEqual(rejected(result).code, "process_drift")
        self.assertEqual([call.args[1] for call in signals.call_args_list], [signal.SIGSTOP, signal.SIGCONT])
        self.assertIsNone(child.poll())

    def test_actual_descendant_fork_after_snapshot_fails_before_signal(self) -> None:
        script = "import os,sys,time; sys.stdin.readline(); child=os.fork(); print(child,flush=True) if child else None; time.sleep(120)"
        parent = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.children.append(parent)
        pins = self.pins(parent.pid)
        assert parent.stdin is not None and parent.stdout is not None
        parent.stdin.write(b"fork\n")
        parent.stdin.flush()
        descendant_pid = int(parent.stdout.readline())
        self.addCleanup(lambda: os.kill(descendant_pid, signal.SIGKILL) if process(descendant_pid, "host") else None)
        result = stop_domain(pins, parent.pid, ())
        self.assertIsInstance(result, Rejected)
        self.assertEqual(rejected(result).code, "process_drift")
        self.assertIsNone(parent.poll())

    def test_process_gone_is_not_implied_by_changed_argv(self) -> None:
        child = self.child()
        pins = self.pins(child.pid)
        wrong = (replace(pins[0], argv_sha256="0" * 64),)
        result = old_processes_gone(wrong)
        self.assertIsInstance(result, Rejected)
        self.assertEqual(rejected(result).code, "native_process_changed")

    def test_private_configuration_rejects_other_fields_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backend.json"
            value = {"old_session_id": "old", "host_pid": 123, "bridge_state_path": str(Path(directory) / "bridge"), "host_config_path": str(Path(directory) / "identity"), "server_url": "http://127.0.0.1:6767"}
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            self.assertNotIsInstance(read_config(path), Rejected)
            path.chmod(0o644)
            self.assertEqual(rejected(read_config(path)).code, "unsafe_backend_config")
            path.chmod(0o600)
            path.write_text(json.dumps(value | {"arbitrary_command": ["unsafe"]}))
            self.assertEqual(rejected(read_config(path)).code, "invalid_backend_config")

    def test_unrecorded_host_standby_exits_on_parent_pipe_eof(self) -> None:
        result = subprocess.run([sys.executable, "-m", "omo_manager.omo_omnigent_backend", "--host-standby", "http://127.0.0.1:1", "a" * 64], input=b"", capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)

    def test_host_standby_executes_only_after_exact_release(self) -> None:
        for body, called in ((b"wrong\n", False), (b"a" * 64 + b"\n", True)):
            with patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(body))), patch("os.execv") as execute:
                host_standby("http://127.0.0.1:1", "a" * 64)
            self.assertEqual(execute.called, called)
            if called:
                self.assertEqual(execute.call_args.args[1], [sys.executable, "-m", "omnigent.cli", "host", "--server", "http://127.0.0.1:1", "--non-interactive"])

    def test_loopback_url_cannot_hide_remote_credentials_or_path(self) -> None:
        self.assertEqual(local_server_port("http://127.0.0.1:6767"), 6767)
        for value in ("http://127.0.0.1:80@remote/", "http://user@127.0.0.1:80", "http://127.0.0.1:80/path", "http://127.0.0.1:80?q=x", "http://127.0.0.1:0", "http://127.0.0.1:65536"):
            self.assertIsInstance(local_server_port(value), Rejected, value)

    def test_host_tunnel_peer_must_belong_to_exact_spawned_process(self) -> None:
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(3)
        port = listener.getsockname()[1]
        script = "import socket,sys; peer=socket.create_connection(('127.0.0.1',int(sys.argv[1]))); print('ready',flush=True); sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", script, str(port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.children.append(child)
        connection, peer = listener.accept()
        self.addCleanup(connection.close)
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline(), b"ready\n")
        pin = running(child.pid, "host").pin
        self.assertIsNone(verify_host_socket(pin, peer, port))
        other = subprocess.Popen([sys.executable, "-c", script, str(port)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.children.append(other)
        foreign, _ = listener.accept()
        self.addCleanup(foreign.close)
        assert other.stdout is not None
        self.assertEqual(other.stdout.readline(), b"ready\n")
        other_pin = running(other.pid, "host").pin
        self.assertEqual(rejected(verify_host_socket(other_pin, peer, port)).code, "foreign_host_connection")


class NativeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.calls: list[str] = []
        self.turns: list[object] = []
        self.workspace = "test-workspace"
        self.settings: dict[str, object] = {"thread": {"id": "new-thread", "turns": [], "cwd": self.workspace}, "cwd": self.workspace, "sandbox": {"type": "dangerFullAccess"}, "approvalPolicy": "never", "model": "gpt-6-astra", "reasoningEffort": "ultra"}
        self.server: Server | None = None
        self.url = ""

    async def asyncSetUp(self) -> None:
        import websockets

        async def endpoint(websocket):
            async for data in websocket:
                message = json.loads(data)
                self.calls.append(message["method"])
                if "id" not in message:
                    continue
                method = message["method"]
                result = {"thread": {"id": "new-thread", "turns": self.turns, "cwd": self.workspace}} if method == "thread/read" else self.settings if method == "thread/resume" else {}
                await websocket.send(json.dumps({"id": message["id"], "result": result}))

        self.server = await websockets.serve(endpoint, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{tuple(self.server.sockets)[0].getsockname()[1]}"

    async def asyncTearDown(self) -> None:
        present(self.server).close()
        await present(self.server).wait_closed()

    async def test_empty_new_thread_settings_are_verified_without_work(self) -> None:
        result = await verify_empty_native_thread(self.url, "new-thread", self.workspace)
        self.assertNotIsInstance(result, Rejected)
        self.assertEqual(self.calls, ["initialize", "initialized", "thread/read", "thread/resume", "thread/read"])

    async def test_existing_turns_reject_before_resume(self) -> None:
        self.turns = [{"id": "consumed-turn", "status": "completed"}]
        result = await verify_empty_native_thread(self.url, "new-thread", self.workspace)
        self.assertEqual(rejected(result).code, "new_native_history_mismatch")
        self.assertNotIn("thread/resume", self.calls)

    async def test_readonly_native_settings_reject_even_when_launch_label_was_requested(self) -> None:
        self.settings["sandbox"] = {"type": "readOnly", "networkAccess": False}
        result = await verify_empty_native_thread(self.url, "new-thread", self.workspace)
        self.assertEqual(rejected(result).code, "new_native_settings_mismatch")
        self.assertNotIn("turn/start", self.calls)

    async def test_wrong_model_effort_or_workspace_rejects(self) -> None:
        for key, value in (("model", "wrong"), ("reasoningEffort", "low"), ("cwd", "elsewhere")):
            previous = self.settings[key]
            self.settings[key] = value
            result = await verify_empty_native_thread(self.url, "new-thread", self.workspace)
            self.assertEqual(rejected(result).code, "new_native_settings_mismatch")
            self.settings[key] = previous


class ReservedStoreTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.directory.chmod(0o700)
        uri = f"sqlite:///{self.directory / 'application.db'}"
        agents = SqlAlchemyAgentStore(uri)
        (self.directory / "bundle").write_bytes(b"generic native Codex wrapper")
        agents.create(AGENT, "codex-native-ui", "bundle")
        self.fence = FenceStore(self.directory / "fence.db")
        app = FastAPI()
        app.state.host_registry, app.state.tunnel_registry, app.state.host_store, app.state.agent_store = None, None, None, agents
        application = BackendResources(app, self.fence, uri, None, str(self.directory), lambda: "1" * 64)
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        self.backend = OmnigentBackend(application, loop, self.directory / "config", BackendConfig(OLD, 999, "unused-bridge", "unused-identity", "http://127.0.0.1:6767"))
        self.backend.store.create_conversation(conversation_id=OLD, agent_id=AGENT, runner_id="old-runner", host_id=HOST, workspace=str(self.directory), title="paper")
        self.backend.store.set_labels(OLD, {"omnigent.ui": "terminal", "omnigent.wrapper": "codex-native-ui"})
        agent = self.backend._agent(AGENT)
        binding = self.directory / "binding"
        binding.write_bytes(b"human")
        image = FileImage.capture(binding)
        runtime = RuntimeState(OLD, "old-runner", HOST, "old-thread", encoded(b"{}"), encoded(b"[]"), encoded(b"{}"), encoded(canonical({"agent": agent, "files": {"fixture": "same"}}).encode()), (), ("old-runner",), (), 0, 0, str(self.directory))
        self.packet = ExactPacket("operation", NEW, "manifest-once", image, image, image, WorkspacePin.capture(self.directory), runtime, (), "1" * 64, encoded(b"nine unchanged items"), tuple(f"item {index}" for index in range(9)), (), (), image.data_b64, image.data_b64, "Only the still-open paper work.")
        self.expected = self.packet.expected
        self.spec = ReplacementSpec("operation", NEW, digest(b"human"), self.packet.sha256, self.expected)
        self.assertNotIsInstance(self.fence.prepare(self.spec, self.expected, b"human", self.packet.serialize()), Rejected)
        self.assertNotIsInstance(self.fence.fence("operation", self.expected), Rejected)
        receipt: dict[str, object] = {"operation_id": "operation", **{key: getattr(self.expected, key) for key in ("session_id", "runner_id", "thread_id", "process_sha256", "host_sha256")}, "native_writers_gone": True, "pending_launches_absent": True, "transport_drained": True, "evidence_sha256": "2" * 64}
        self.assertNotIsInstance(self.fence.advance("operation", "old_quiesced", receipt), Rejected)

    def setUp(self) -> None:
        self.enterContext(patch("omo_manager.omo_omnigent_replace.AUTHORITY_SHA256", digest(b"human")))

    def test_reserved_successor_uses_real_store_and_preserves_old_owner(self) -> None:
        old = asdict(present(self.backend.store.get_conversation(OLD)))
        result = self.backend.prepare_successor(self.packet)
        self.assertNotIsInstance(result, Rejected)
        successor = self.backend.store.get_conversation(NEW)
        assert successor is not None
        self.assertEqual(successor.model_override, "gpt-6-astra")
        self.assertEqual(successor.reasoning_effort, "ultra")
        self.assertIsNone(successor.external_session_id)
        self.assertIsNone(successor.runner_id)
        self.assertEqual(asdict(present(self.backend.store.get_conversation(OLD))), old)
        self.assertEqual(rejected(self.backend.prepare_successor(self.packet)).code, "successor_exists")

    def test_crash_after_creation_receipt_has_explicit_safe_reconciliation(self) -> None:
        with patch.object(self.backend.store, "update_conversation", side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.backend.prepare_successor(self.packet)
        self.assertEqual(rejected(self.backend.inspect_receipt(self.packet, "new_prepared")).code, "owned_successor_incomplete")
        result = self.backend.reconcile_effect(self.packet, "new_prepared")
        self.assertNotIsInstance(result, Rejected)
        self.assertEqual(len(self.backend.store.list_conversations(kind=None).data), 2)

    def test_atomic_creation_nonce_recovers_crash_before_identity_receipt(self) -> None:
        record = self.fence.record_evidence

        def crash(operation_id, key, receipt):
            if key == "successor_created":
                raise OSError("crash after public store creation")
            return record(operation_id, key, receipt)

        with patch.object(self.fence, "record_evidence", side_effect=crash):
            with self.assertRaises(OSError):
                self.backend.prepare_successor(self.packet)
        self.assertEqual(rejected(self.backend.inspect_receipt(self.packet, "new_prepared")).code, "owned_successor_incomplete")
        self.assertNotIsInstance(self.backend.reconcile_effect(self.packet, "new_prepared"), Rejected)
        self.assertEqual(present(self.backend.store.get_conversation(NEW)).title, "paper")

    def test_crash_between_ap_and_metadata_uses_exact_empty_row_rollback(self) -> None:
        session = self.backend.store._session

        def fail_metadata(name):
            if name == "insert_conversation_metadata":
                raise OSError("injected metadata transaction crash")
            return session(name)

        with patch.object(self.backend.store, "_session", side_effect=fail_metadata):
            with self.assertRaises(OSError):
                self.backend.prepare_successor(self.packet)
        self.assertIsNone(present(self.backend.store.get_conversation(NEW)).host_id)
        self.assertEqual(rejected(self.backend.inspect_receipt(self.packet, "new_prepared")).code, "owned_successor_incomplete")
        with patch.object(self.backend, "_run", side_effect=asyncio.run):
            result = self.backend.reconcile_effect(self.packet, "new_prepared")
        self.assertNotIsInstance(result, Rejected)
        self.assertEqual(present(self.backend.store.get_conversation(NEW)).host_id, HOST)
        self.assertIsNotNone(self.backend.store.get_conversation(OLD))
        self.assertEqual(len(self.backend.store.list_conversations(kind=None).data), 2)

    def test_foreign_creation_nonce_is_not_adopted(self) -> None:
        self.backend.store.create_conversation(conversation_id=NEW, agent_id=AGENT, host_id=HOST, workspace=str(self.directory), title="omo-replacement:operation:foreign")
        self.assertEqual(rejected(self.backend.reconcile_effect(self.packet, "new_prepared")).code, "creation_outcome_unknown")

    def test_partial_successor_with_foreign_metadata_is_never_repaired(self) -> None:
        with patch.object(self.backend.store, "update_conversation", side_effect=OSError("injected crash")):
            with self.assertRaises(OSError):
                self.backend.prepare_successor(self.packet)
        self.backend.store.update_conversation(NEW, title="foreign owner")
        before = asdict(present(self.backend.store.get_conversation(NEW)))
        result = self.backend.reconcile_effect(self.packet, "new_prepared")
        self.assertEqual(rejected(result).code, "successor_creation_drift")
        self.assertEqual(asdict(present(self.backend.store.get_conversation(NEW))), before)

    def test_same_id_without_creation_receipt_is_not_adopted(self) -> None:
        self.backend.store.create_conversation(conversation_id=NEW, agent_id=AGENT, host_id=HOST, workspace=str(self.directory))
        self.assertEqual(rejected(self.backend.reconcile_effect(self.packet, "new_prepared")).code, "creation_outcome_unknown")

    def test_agent_bundle_drift_rejects_before_any_successor_write(self) -> None:
        (self.directory / "bundle").write_bytes(b"changed instructions")
        result = self.backend.prepare_successor(self.packet)
        self.assertEqual(rejected(result).code, "agent_drift")
        self.assertIsNone(self.backend.store.get_conversation(NEW))

    def test_reconciliation_never_launches_before_committed_phase(self) -> None:
        with patch.object(self.backend, "_restart_host") as restart:
            result = self.backend.reconcile_effect(self.packet, "delivery_confirmed")
        self.assertEqual(rejected(result).code, "operation_mismatch")
        restart.assert_not_called()

    def committed(self) -> None:
        prepared = self.backend.prepare_successor(self.packet)
        assert not isinstance(prepared, Rejected), prepared
        self.assertNotIsInstance(self.fence.advance("operation", "new_prepared", prepared), Rejected)
        self.assertNotIsInstance(self.fence.advance("operation", "committed", {"operation_id": "operation", "new_session_id": NEW, "evidence_sha256": "3" * 64}), Rejected)
        self.assertNotIsInstance(self.fence.claim_delivery("operation", self.packet.delivery_id, digest(self.packet.prompt.encode())), Rejected)

    def claim_manifest(self) -> None:
        self.backend.store.set_runner_id(NEW, "new-runner")
        self.assertNotIsInstance(self.fence.claim_successor_launch(HOST, NEW, "new-runner", "request"), Rejected)
        self.fence.record_evidence("operation", "native_successor_verified", {"operation_id": "operation", "thread_id": "new-thread", "evidence_sha256": "4" * 64})
        self.assertNotIsInstance(self.fence.claim_manifest_dispatch("operation", self.packet.delivery_id, digest(self.packet.prompt.encode()), "new-runner", "new-thread"), Rejected)

    def mirror(self, text: str) -> None:
        from omnigent.entities import NewConversationItem, parse_item_data

        self.backend.store.append(NEW, [NewConversationItem(type="message", response_id="", data=parse_item_data("message", {"role": "user", "content": [{"type": "input_text", "text": text}]}))])

    def test_pre_dispatch_inspection_is_readonly_and_reconciliation_explicit(self) -> None:
        self.committed()
        before = self.fence.get("operation")
        self.assertEqual(rejected(self.backend.inspect_receipt(self.packet, "delivery_confirmed")).code, "owned_delivery_pre_dispatch")
        self.assertEqual(self.fence.get("operation"), before)
        with patch.object(self.backend, "deliver", return_value=Rejected("controlled_stop", "no effects in fixture")) as delivery:
            result = self.backend.reconcile_effect(self.packet, "delivery_confirmed")
        self.assertEqual(rejected(result).code, "controlled_stop")
        delivery.assert_called_once_with(self.packet)

    def test_crash_after_dispatch_claim_never_restarts_or_resends(self) -> None:
        self.committed()
        self.claim_manifest()
        with patch.object(self.backend, "_restart_host") as restart, patch.object(self.backend, "_launch_and_deliver") as delivery:
            self.assertEqual(rejected(self.backend.deliver(self.packet)).code, "delivery_unknown")
            self.assertEqual(rejected(self.backend.reconcile_effect(self.packet, "delivery_confirmed")).code, "delivery_unknown")
        restart.assert_not_called()
        delivery.assert_not_called()

    def test_single_mirrored_manifest_is_required_and_duplicate_is_unknown(self) -> None:
        self.committed()
        self.claim_manifest()
        self.mirror(self.packet.prompt)
        self.backend.application.inner_app.state.runner_router = SimpleNamespace(runner_is_online=lambda runner: runner == "new-runner")
        self.assertNotIsInstance(self.backend.inspect_receipt(self.packet, "delivery_confirmed"), Rejected)
        self.mirror(self.packet.prompt)
        self.assertEqual(rejected(self.backend.inspect_receipt(self.packet, "delivery_confirmed")).code, "delivery_unknown")

    def test_stale_native_replay_directory_rejects_before_any_launch(self) -> None:
        self.committed()
        root = self.directory / "bridges"
        root.mkdir()
        with patch("omnigent.codex_native_bridge._BRIDGE_ROOT", root):
            from omnigent.codex_native_bridge import bridge_dir_for_bridge_id

            bridge = bridge_dir_for_bridge_id(NEW)
            bridge.mkdir()
            (bridge / "turn_replay_pending.json").write_text(json.dumps({"session_id": NEW, "prompt": "stale work"}))
            with patch.object(self.backend, "_restart_host") as restart, patch.object(self.backend, "_launch_and_deliver") as native:
                self.assertEqual(rejected(self.backend.deliver(self.packet)).code, "successor_bridge_exists")
            restart.assert_not_called()
            native.assert_not_called()
            self.assertEqual(rejected(self.backend._bridge_gate(self.packet, first_launch=False)).code, "successor_replay_artifact")

    def test_bridge_alias_is_rejected_even_when_its_directory_is_absent(self) -> None:
        self.committed()
        self.backend.store.set_labels(OLD, {"omnigent.codex_native.bridge_id": NEW})
        with patch("omnigent.codex_native_bridge._BRIDGE_ROOT", self.directory / "bridges"):
            self.assertEqual(rejected(self.backend._bridge_gate(self.packet, first_launch=True)).code, "successor_bridge_alias")

    def test_claimed_runner_recovery_uses_original_once_and_marks_before_post(self) -> None:
        self.committed()
        self.backend.store.set_runner_id(NEW, "new-runner")
        self.backend.store.set_external_session_id(NEW, "new-thread")
        self.fence.claim_successor_launch(HOST, NEW, "new-runner", "request")
        pin = running(os.getpid(), "replacement-host").pin
        self.fence.record_evidence("operation", "host_restarted", {"operation_id": "operation", "process": asdict(pin), "evidence_sha256": "4" * 64})
        connection = SimpleNamespace(pending_launches={})
        self.backend.hosts = SimpleNamespace(get=lambda host: connection, send_text=Mock())
        self.backend.runners = SimpleNamespace(online_runner_ids=lambda: ["new-runner"], wait_for_runner=AsyncMock(return_value=object()))
        sends = []

        async def post(path, **kwargs):
            self.assertIn("manifest_dispatch_intent", present(self.fence.get("operation")).receipts)
            sends.append(kwargs["json"])
            raise OSError("crash after possible runner acceptance")

        route = patch.object(self.backend.router, "client_for_conversation", return_value=SimpleNamespace(client=SimpleNamespace(post=post)))
        bridge = SimpleNamespace(session_id=NEW, thread_id="new-thread", active_turn_id=None, socket_path="ws://127.0.0.1:1")

        async def run():
            self.backend.loop = asyncio.get_running_loop()
            with route, patch.object(self.backend, "_host_connection_pin", return_value=None), patch("omnigent.codex_native_bridge._BRIDGE_ROOT", self.directory / "bridges"), patch("omnigent.codex_native_bridge.read_bridge_state", return_value=bridge), patch("omnigent.server.runner_session_init.RunnerSessionInitializer.initialize", new=AsyncMock(return_value=SimpleNamespace(status_code=200))), patch("omo_manager.omo_omnigent_backend.verify_empty_native_thread", new=AsyncMock(return_value={"thread_id": "new-thread"})):
                with self.assertRaises(OSError):
                    await self.backend._launch_and_deliver(self.packet)
                self.assertEqual(rejected((await self.backend._launch_and_deliver(self.packet))).code, "delivery_unknown")

        asyncio.run(run())
        self.assertEqual(sends, [{"role": "user", "content": [{"type": "input_text", "text": self.packet.prompt}]}])
        self.backend.hosts.send_text.assert_not_called()

    def test_host_exec_before_restart_receipt_promotes_only_same_incarnation(self) -> None:
        self.committed()
        argv = [sys.executable, "-m", "omnigent.cli", "host", "--server", self.backend.config.server_url, "--non-interactive"]
        actual = running(os.getpid(), "replacement-host")
        self.fence.record_evidence("operation", "host_restart_intent", {"operation_id": "operation", "argv": argv, "evidence_sha256": "4" * 64})
        self.fence.record_evidence("operation", "host_restart_standby", {"operation_id": "operation", "process": asdict(actual.pin), "argv": argv, "evidence_sha256": "4" * 64})
        routing = json.loads(decoded(self.packet.runtime.routing_b64))
        self.backend.hosts = SimpleNamespace(get=lambda host: object())
        executed = replace(actual, argv=tuple(argv), pin=replace(actual.pin, argv_sha256=digest(b"fixture exec")))
        with patch.object(self.backend, "_files", return_value=routing["files"]), patch("omo_manager.omo_omnigent_backend.process", return_value=executed), patch.object(self.backend, "_host_connection_pin", return_value=None), patch("subprocess.Popen") as spawn:
            self.assertIsNone(self.backend._restart_host(self.packet))
        spawn.assert_not_called()
        self.assertEqual(present(self.fence.get("operation")).receipts["host_restarted"]["process"], asdict(executed.pin))


if __name__ == "__main__":
    unittest.main()
