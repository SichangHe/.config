"""Server-only handover against isolated processes, stores, and native reads."""

# pyright: reportUninitializedInstanceVariable=false

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omo_manager import omo_omnigent_refresh as refresh
from omo_manager.omo_omnigent_backend import BackendConfig, Process, process, same_process
from omo_manager.omo_omnigent_fence import Rejected, digest
from omo_manager.omo_omnigent_replace import OLD_HOST_ID, OLD_SESSION_ID, OLD_THREAD_ID, ProcessPin
from omo_manager.omo_task_lock import task_file_lock
from websockets.asyncio.server import ServerConnection, serve


def rejection(value: object) -> str:
    assert isinstance(value, Rejected), value
    return value.code


# 🧑 "preserving the task and queue and delivering only open work."
class RefreshProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="omnigent-refresh-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.children: list[subprocess.Popen[bytes]] = []
        self.forks: list[ProcessPin] = []
        self.addCleanup(self.reap)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.old = self.sleeper("server")
        self.host = subprocess.Popen((sys.executable, "-c", "import os,signal,time; signal.signal(signal.SIGCHLD,signal.SIG_IGN); child=os.fork(); print(child,flush=True) if child else None; time.sleep(120)"), stdout=subprocess.PIPE)
        self.children.append(self.host)
        assert self.host.stdout is not None
        self.assertTrue(select.select([self.host.stdout], [], [], 3)[0])
        native_pid = int(self.host.stdout.readline())
        self.host.stdout.close()
        self.forks.append(self.pin(native_pid, "native"))
        self.task = self.root / refresh.TASK_NAME
        queue = "".join(f"  - preserved task {index}\n" for index in range(9))
        self.task.write_text(f"---\nversion: v1.0.0\nstatus: blocked\nblocked_on: review.md\nrunat: omnigent://{OLD_SESSION_ID}\ntool: codex\nmanagerat: dw:59\nis_manager: false\npending_task_items:\n{queue}---\nPreserved task.\n")
        self.task.chmod(0o600)
        self.task_bytes = self.task.read_bytes()
        self.marker = self.root / "replacement-ready"
        workspace = self.root / refresh.WORKSPACE_NAME
        workspace.mkdir()
        names = ("authority_path", "todo_path", "global_config_path", "fence_database", "control_socket", "preparation_path", "packet_path", "approval_path", "backend_config_path", "journal_path", "log_path")
        request = refresh.RefreshRequest(
            server_pid=self.old.pid, checkout=str(self.root), published_commit="a" * 40,
            published_ref="refs/heads/fixture", task_path=str(self.task), workspace=str(workspace),
            old_native_cwd=str(self.root), old_bridge_cwd=None,
            **{name: str(self.root / ("config.yaml" if name == "global_config_path" else name)) for name in names},
        )
        config = BackendConfig(OLD_SESSION_ID, self.host.pid, str(self.root / "bridge"), str(self.root / "host-config"), f"http://127.0.0.1:{self.port}")
        local_config = self.root / "local.env"
        paths = (request.authority_path, request.task_path, request.todo_path, request.global_config_path, request.preparation_path, request.backend_config_path, config.host_config_path, str(local_config))
        for path in paths:
            if path != request.task_path:
                Path(path).write_bytes(refresh._bytes(asdict(config)) if path == request.backend_config_path else b"fixture preserved input")
                Path(path).chmod(0o600)
        Path(request.global_config_path).write_text(json.dumps({"host": {"host_id": OLD_HOST_ID}}))
        local_config.write_text(f"OMO_SOURCE1957_WORKSPACE={workspace}\n")
        self.todo_bytes = Path(request.todo_path).read_bytes()
        self.before = refresh.PreservedState(
            (self.pin(self.host.pid, "host"), self.pin(native_pid, "native")),
            digest(b"four sessions and 22 items"), digest(b"idle native thread"),
            digest(b"bridge"), digest(b"public auth and runner"), "runner-fixture",
        )
        executable = str(Path(sys.executable).resolve())
        new_argv = (sys.executable, "-c", "import os,pathlib,socket,sys,time; listener=socket.socket(); listener.bind(('127.0.0.1',int(sys.argv[2]))); listener.listen(); marker=pathlib.Path(sys.argv[1]); marker.with_name('environment').write_text(os.environ['OMO_REFRESH_SENTINEL']); marker.write_text(str(os.getpid())); time.sleep(120)", str(self.marker), str(self.port))
        self.plan = refresh.RefreshPlan(
            request, self.pin(self.old.pid, "server"), executable, digest(Path(executable).read_bytes()),
            self.old_process().argv, new_argv, tuple(refresh._snapshot(path) for path in paths),
            digest(b"published fixture"), refresh.OMNIGENT_SOURCE_SHA256, digest(refresh.raw_queue(self.task_bytes)), self.before,
            old_environment_sha256=digest(Path(f"/proc/{self.old.pid}/environ").read_bytes()),
        )
        self.plan_path = self.root / "plan.json"
        self.review_path = self.root / "review.txt"
        self.publish(self.plan)
        self.enterContext(patch.object(refresh, "_published", return_value=None))
        self.enterContext(patch.object(refresh, "_source_hash", return_value=self.plan.sources_sha256))
        self.enterContext(patch.object(refresh, "validate_package", return_value=self.plan.package_sha256))
        self.enterContext(patch.object(refresh, "observe", return_value=self.before))
        self.enterContext(patch.object(refresh, "_old_command", return_value=(self.plan.old_argv[9], str(self.root), self.port)))
        self.enterContext(patch.object(refresh, "_command", return_value=self.plan.new_argv))
        self.enterContext(patch.object(refresh, "AUTHORITY_SHA256", self.plan.files[0].sha256))
        self.enterContext(patch.object(refresh, "configuration_source", return_value=local_config))
        self.enterContext(patch.object(refresh, "configured_workspace", return_value=workspace))

    def sleeper(self, name: str) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            (sys.executable, "-c", "import socket,sys,time; listener=socket.socket(); listener.bind(('127.0.0.1',int(sys.argv[5]))); listener.listen(); print('ready',flush=True); time.sleep(120)", name, "--host", "127.0.0.1", "--port", str(self.port), "--database-uri", f"sqlite:///{self.root / 'fixture.sqlite'}", "--artifact-location", str(self.root)),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"HOME": str(self.root), "PATH": os.defpath, "OMO_REFRESH_SENTINEL": "preserved fixture environment", "OMNIGENT_CONFIG_HOME": str(self.root), "OMO_MANAGER_LOCAL_ENV": str(self.root / "local.env")},
        )
        self.children.append(child)
        assert child.stdout is not None
        self.assertTrue(select.select([child.stdout], [], [], 3)[0])
        self.assertEqual(child.stdout.readline(), b"ready\n")
        child.stdout.close()
        return child

    def pin(self, pid: int, domain: str) -> ProcessPin:
        value = process(pid, domain)
        assert isinstance(value, Process), value
        return value.pin

    def old_process(self) -> Process:
        value = process(self.old.pid, "server")
        assert isinstance(value, Process), value
        return value

    def reap(self) -> None:
        path = self.root / "journal_path"
        if path.exists():
            _, journal = refresh._journal(path)
            if journal.child is not None and journal.child.pid != os.getpid():
                self.forks.append(journal.child)
        for pin in reversed(self.forks):
            value = process(pin.pid, pin.writer_domain)
            if not isinstance(value, Process) or (value.pin.start_ticks, value.pin.boot_id) != (pin.start_ticks, pin.boot_id):
                continue
            try:
                os.kill(pin.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pin.pid, 0)
            except ChildProcessError:
                pass
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)

    def publish(self, plan: refresh.RefreshPlan) -> None:
        self.plan = plan
        payload = refresh._bytes(asdict(plan))
        self.plan_path.write_bytes(payload)
        self.plan_path.chmod(0o600)
        self.plan_sha256 = digest(payload)
        review = f"approve Source1957 server refresh {self.plan_sha256}\n".encode()
        self.review_path.write_bytes(review)
        self.review_path.chmod(0o600)
        self.review_sha256 = digest(review)

    def arguments(self) -> tuple[Path, str, Path, str]:
        return self.plan_path, self.plan_sha256, self.review_path, self.review_sha256

    def record(self) -> None:
        record = refresh.Journal(self.plan_sha256, self.review_sha256, "launch_intent")
        refresh.create_snapshot(Path(self.plan.request.journal_path), refresh._bytes(asdict(record)), 0o600)

    def spawn_standby(self, *args: object, **kwargs: object) -> SimpleNamespace:
        expected_environment = {os.fsdecode(key): os.fsdecode(value) for key, value in refresh._server_environment(self.old.pid)[0].items()}
        self.assertTrue(kwargs["env"] == expected_environment, "standby must inherit the original environment")
        with (self.root / "launches").open("ab", buffering=0) as stream:
            stream.write(b"1")
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                result = refresh.standby(*self.arguments())
                (self.root / f"standby-error-{os.getpid()}").write_text(repr(result))
            finally:
                os._exit(2)
        self.forks.append(self.pin(pid, "refresh_standby"))
        return SimpleNamespace(pid=pid, wait=lambda: None)

    def wait_file(self, path: Path) -> None:
        deadline_s = time.monotonic() + 8
        while not path.exists() and time.monotonic() < deadline_s:
            time.sleep(0.01)
        self.assertTrue(path.exists(), f"fixture did not produce {path.name}")

    def assert_preserved(self) -> None:
        for pin in self.before.processes:
            self.assertTrue(same_process(process(pin.pid, pin.writer_domain), pin))
        self.assertEqual(self.task.read_bytes(), self.task_bytes)
        self.assertEqual(Path(self.plan.request.todo_path).read_bytes(), self.todo_bytes)

    def test_prepare_and_check_only_observe_existing_state(self) -> None:
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch.object(refresh.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as bootstrap:
            plan = refresh.prepare(self.plan.request)
        self.assertEqual(plan, self.plan)
        bootstrap.assert_called_once()
        call = bootstrap.call_args
        assert call is not None
        self.assertEqual(call.args, ((*self.plan.new_argv, "--check"),))
        expected_environment = {os.fsdecode(key): os.fsdecode(value) for key, value in refresh._server_environment(self.old.pid)[0].items()}
        self.assertTrue(call.kwargs["env"] == expected_environment, "bootstrap must use the exact original environment")
        self.assertEqual({key: value for key, value in call.kwargs.items() if key != "env"}, {"cwd": self.plan.request.checkout, "capture_output": True, "timeout": 30})
        self.assertIsNone(refresh.check(self.plan))
        self.assertEqual(before, {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.assertIsNone(self.old.poll())
        self.assert_preserved()

    def test_real_pidfd_handover_executes_once_and_preserves_host_and_queue(self) -> None:
        self.assertNotIn(b"preserved fixture environment", self.plan_path.read_bytes())
        with patch.object(refresh.subprocess, "Popen", side_effect=self.spawn_standby) as launches:
            result = refresh.execute(*self.arguments(), timeout_s=8)
            assert isinstance(result, refresh.Journal), result
            assert result.child is not None
            self.assertEqual(result.phase, "verified")
            self.assertEqual(self.old.wait(timeout=3), -signal.SIGTERM)
            self.wait_file(self.marker)
            self.assertEqual(int(self.marker.read_text()), result.child.pid)
            self.assertEqual((self.root / "environment").read_text(), "preserved fixture environment")
            self.assertTrue(refresh._child_matches(self.plan, result.child))
            self.assertEqual(refresh.execute(*self.arguments(), timeout_s=0), result)
            self.assertEqual(refresh.reconcile(*self.arguments(), timeout_s=0), result)
            self.assertEqual(launches.call_count, 1)
        self.assert_preserved()

    def test_wrong_original_incarnation_cannot_signal(self) -> None:
        self.publish(replace(self.plan, old_server=replace(self.plan.old_server, start_ticks=self.plan.old_server.start_ticks + 1)))
        self.record()
        with patch.object(refresh, "pidfd_signal", wraps=refresh.pidfd_signal) as signals:
            result = refresh.standby(*self.arguments())
        self.assertEqual(rejection(result), "server_process_drift")
        signals.assert_not_called()
        self.assertIsNone(self.old.poll())
        self.assert_preserved()

    def test_wrong_plan_review_and_mutated_input_cannot_launch(self) -> None:
        with patch.object(refresh.subprocess, "Popen") as launches:
            for arguments in (
                (self.plan_path, "0" * 64, self.review_path, self.review_sha256),
                (self.plan_path, self.plan_sha256, self.review_path, "0" * 64),
            ):
                self.assertIsInstance(refresh.execute(*arguments, timeout_s=0), Rejected)
            self.task.write_bytes(self.task_bytes + b"unexpected work\n")
            result = refresh.execute(*self.arguments(), timeout_s=0)
            self.assertEqual(rejection(result), "refresh_input_drift")
            launches.assert_not_called()
        self.assertFalse(Path(self.plan.request.journal_path).exists())
        self.assertIsNone(self.old.poll())

    def test_predecessor_cwd_bindings_are_explicit_and_request_tampering_cannot_launch(self) -> None:
        request = asdict(self.plan.request)
        self.assertIsNone(request["old_bridge_cwd"])
        self.assertNotEqual(request["old_native_cwd"], request["workspace"])
        self.assertEqual(TypeAdapter(refresh.RefreshRequest).validate_json(refresh._bytes(request)), self.plan.request)
        for missing in ("old_native_cwd", "old_bridge_cwd"):
            incomplete = {key: value for key, value in request.items() if key != missing}
            with self.assertRaises(ValidationError):
                TypeAdapter(refresh.RefreshRequest).validate_json(refresh._bytes(incomplete))
        for field in ("old_native_cwd", "old_bridge_cwd"):
            tampered = replace(self.plan, request=replace(self.plan.request, **{field: self.plan.request.workspace}))
            self.plan_path.write_bytes(refresh._bytes(asdict(tampered)))
            with patch.object(refresh.subprocess, "Popen") as launches, patch.object(refresh, "pidfd_signal") as signals:
                self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=0)), "refresh_plan_mismatch")
                launches.assert_not_called()
                signals.assert_not_called()
        self.assertIsNone(self.old.poll())
        self.assertFalse(Path(self.plan.request.journal_path).exists())

    def test_observed_predecessor_cwd_never_substitutes_for_successor_workspace(self) -> None:
        wrong = replace(self.plan.request, workspace=self.plan.request.old_native_cwd)
        self.assertEqual(rejection(refresh._scope(wrong)), "wrong_refresh_scope")
        for cwd in ("relative", str(self.root / "missing")):
            result = refresh._scope(replace(self.plan.request, old_native_cwd=cwd))
            self.assertIsInstance(result, Rejected)

    def test_publication_package_and_preserved_history_drift_cannot_launch(self) -> None:
        cases = (
            ("_published", Rejected("unpublished_sources", "fixture origin moved"), "unpublished_sources"),
            ("_source_hash", digest(b"different published code"), "refresh_input_drift"),
            ("validate_package", digest(b"different installed package"), "package_drift"),
            ("observe", replace(self.before, native_thread_sha256=digest(b"different native history")), "preserved_state_drift"),
        )
        with patch.object(refresh.subprocess, "Popen") as launches:
            for name, changed, expected in cases:
                with self.subTest(name=name), patch.object(refresh, name, return_value=changed):
                    self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=0)), expected)
            launches.assert_not_called()
        self.assertIsNone(self.old.poll())
        self.assert_preserved()

    def test_input_changed_during_pidfd_binding_cannot_signal(self) -> None:
        self.record()
        actual_open = refresh.pidfd_open

        def drift(pid: int) -> int:
            handle = actual_open(pid)
            Path(self.plan.request.authority_path).write_bytes(b"changed after initial check")
            return handle

        with patch.object(refresh, "pidfd_open", side_effect=drift), patch.object(refresh, "pidfd_signal") as signals:
            result = refresh.standby(*self.arguments())
        self.assertEqual(rejection(result), "refresh_input_drift")
        signals.assert_not_called()
        self.assertIsNone(self.old.poll())

    def test_canonical_plan_and_separate_review_are_required(self) -> None:
        original = self.plan_path.read_bytes()
        self.plan_path.write_bytes(original + b"\n")
        result = refresh._load(self.plan_path, digest(original + b"\n"))
        self.assertEqual(rejection(result), "refresh_plan_mismatch")
        self.plan_path.write_bytes(original)
        self.review_path.write_text(f"approve Source1957 server refresh {'b' * 64}\n")
        result = refresh._review(self.review_path, digest(self.review_path.read_bytes()), self.plan_sha256)
        self.assertEqual(rejection(result), "refresh_review_mismatch")

    def test_recorded_launch_failure_or_missing_child_never_respawns(self) -> None:
        with patch.object(refresh.subprocess, "Popen", side_effect=OSError("fixture spawn uncertainty")) as launches:
            first = refresh.execute(*self.arguments(), timeout_s=0)
            self.assertEqual(rejection(first), "refresh_execute_uncertain")
            self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=0)), "refresh_pending")
            self.assertEqual(rejection(refresh.reconcile(*self.arguments(), timeout_s=0)), "refresh_pending")
            launches.assert_called_once()
        path = Path(self.plan.request.journal_path)
        snapshot, journal = refresh._journal(path)
        refresh._record(snapshot, journal, child=replace(self.plan.old_server, start_ticks=0), phase="starting")
        with patch.object(refresh.subprocess, "Popen") as launches:
            self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=0)), "refresh_child_uncertain")
            launches.assert_not_called()
        self.assertIsNone(self.old.poll())

    def test_journal_from_another_review_is_not_adopted(self) -> None:
        self.record()
        snapshot, journal = refresh._journal(Path(self.plan.request.journal_path))
        refresh._record(snapshot, journal, review_sha256="f" * 64)
        with patch.object(refresh.subprocess, "Popen") as launches, patch.object(refresh, "pidfd_signal") as signals:
            self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=0)), "refresh_journal_mismatch")
            self.assertEqual(rejection(refresh.standby(*self.arguments())), "refresh_already_claimed")
            launches.assert_not_called()
            signals.assert_not_called()

    def test_concurrent_execute_claims_one_standby(self) -> None:
        read_fd, write_fd = os.pipe()
        supervisors: list[int] = []
        with patch.object(refresh.subprocess, "Popen", side_effect=self.spawn_standby):
            for index in range(2):
                pid = os.fork()
                if pid == 0:
                    os.close(write_fd)
                    os.read(read_fd, 1)
                    result = refresh.execute(*self.arguments(), timeout_s=8)
                    (self.root / f"result-{index}").write_text(repr(result))
                    os._exit(0 if isinstance(result, refresh.Journal) else 2)
                supervisors.append(pid)
                self.forks.append(self.pin(pid, "fixture_supervisor"))
            os.close(read_fd)
            os.write(write_fd, b"11")
            os.close(write_fd)
            self.wait_file(self.root / "result-0")
            self.wait_file(self.root / "result-1")
        _, journal = refresh._journal(Path(self.plan.request.journal_path))
        assert journal.child is not None
        self.forks.append(journal.child)
        for pid in supervisors:
            self.assertEqual(os.waitpid(pid, 0)[1], 0)
        self.assertEqual((self.root / "launches").read_bytes(), b"1")
        self.assertEqual(journal.phase, "verified")
        self.assert_preserved()

    def test_supervisor_death_after_durable_claim_does_not_strand_handover(self) -> None:
        ready, proceed = self.root / "signal-ready", self.root / "signal-proceed"
        signal_pidfd = refresh.pidfd_signal

        def paused_signal(descriptor: int, number: int) -> None:
            ready.touch()
            deadline_s = time.monotonic() + 8
            while not proceed.exists() and time.monotonic() < deadline_s:
                time.sleep(0.01)
            if not proceed.exists():
                os._exit(3)
            signal_pidfd(descriptor, number)

        with patch.object(refresh.subprocess, "Popen", side_effect=self.spawn_standby), patch.object(refresh, "pidfd_signal", side_effect=paused_signal):
            supervisor = os.fork()
            if supervisor == 0:
                result = refresh.execute(*self.arguments(), timeout_s=10)
                os._exit(0 if isinstance(result, refresh.Journal) else 2)
            self.forks.append(self.pin(supervisor, "fixture_supervisor"))
            self.wait_file(ready)
            _, journal = refresh._journal(Path(self.plan.request.journal_path))
            self.assertEqual(journal.phase, "stopping")
            assert journal.child is not None
            self.forks.append(journal.child)
            self.assertTrue(same_process(process(journal.child.pid, journal.child.writer_domain), journal.child))
            self.assertIsNone(self.old.poll())
            os.kill(supervisor, signal.SIGKILL)
            os.waitpid(supervisor, 0)
            proceed.touch()
            self.wait_file(self.marker)
        self.assertEqual(self.old.wait(timeout=3), -signal.SIGTERM)
        result = refresh.reconcile(*self.arguments(), timeout_s=3)
        assert isinstance(result, refresh.Journal), result
        self.assertEqual(result.phase, "verified")
        self.assertEqual((self.root / "launches").read_bytes(), b"1")
        self.assert_preserved()

    def test_standby_crash_after_old_exit_never_replays_launch(self) -> None:
        def crash_before_exec(*_args: object) -> None:
            os._exit(33)

        with patch.object(refresh.subprocess, "Popen", side_effect=self.spawn_standby) as launches, patch.object(refresh.os, "execve", side_effect=crash_before_exec):
            self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=5)), "refresh_child_uncertain")
            self.assertEqual(self.old.wait(timeout=3), -signal.SIGTERM)
            _, journal = refresh._journal(Path(self.plan.request.journal_path))
            self.assertEqual(journal.phase, "starting")
            self.assertFalse(self.marker.exists())
            self.assertEqual(rejection(refresh.execute(*self.arguments(), timeout_s=0)), "refresh_child_uncertain")
            self.assertEqual(rejection(refresh.reconcile(*self.arguments(), timeout_s=0)), "refresh_child_uncertain")
            launches.assert_called_once()
        self.assert_preserved()

    def test_journal_changed_after_exit_cannot_execute(self) -> None:
        self.record()
        real_select = select.select

        def changed_claim(read: list[int], write: list[int], error: list[int]) -> tuple[list[int], list[int], list[int]]:
            result = real_select(read, write, error)
            path = Path(self.plan.request.journal_path)
            with task_file_lock(path):
                snapshot, journal = refresh._journal(path)
                refresh._record(snapshot, journal, plan_sha256="c" * 64)
            return result

        with patch.object(refresh.select, "select", side_effect=changed_claim), patch.object(refresh.os, "execve") as execute:
            result = refresh.standby(*self.arguments())
            execute.assert_not_called()
        self.assertEqual(rejection(result), "refresh_journal_drift")
        self.assert_preserved()


class RefreshObservationTests(unittest.TestCase):
    def test_original_command_accepts_only_exact_loopback_server_shape(self) -> None:
        argv = (sys.executable, "-m", "omnigent.cli", "server", "--host", "127.0.0.1", "--port", "6767", "--database-uri", "sqlite:////fixture/app.sqlite", "--artifact-location", "/fixture/artifacts")
        current = process(os.getpid(), "server")
        assert isinstance(current, Process)
        value = replace(current, argv=argv)
        self.assertEqual(refresh._old_command(value), (argv[9], argv[11], 6767))
        self.assertEqual(rejection(refresh._old_command(replace(value, argv=(*argv[:5], "0.0.0.0", *argv[6:])))), "unexpected_server")
        self.assertEqual(rejection(refresh._old_command(replace(value, argv=(*argv, "--reload")))), "unexpected_server")
        self.assertEqual(rejection(refresh._old_command(replace(value, state="T"))), "unexpected_server")

    def test_actual_store_snapshot_preserves_four_sessions_and_22_items(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omnigent-refresh-store-") as directory:
            root = Path(directory).resolve()
            uri = f"sqlite:///{root / 'app.sqlite'}"
            store = SqlAlchemyConversationStore(uri)
            try:
                old = store.create_conversation(conversation_id=OLD_SESSION_ID, title="original", runner_id="runner-old", host_id=OLD_HOST_ID, workspace=str(root))
                store.set_external_session_id(old.id, OLD_THREAD_ID)
                for index in range(3):
                    store.create_conversation(title=f"other {index}")
                store.append(old.id, [NewConversationItem(type="message", response_id=str(uuid4()), data=MessageData(role="user", content=[{"type": "input_text", "text": f"old item {index}"}])) for index in range(22)])
                store.set_labels(old.id, {"preserved": "label"})
                before = refresh._database(uri, str(root))
                assert isinstance(before, tuple), before
                self.assertEqual(before[1], "runner-old")
                store.touch_runner_liveness(["runner-old"], 123456789)
                self.assertEqual(refresh._database(uri, str(root)), before)
                store.update_conversation(old.id, title="changed history")
                after = refresh._database(uri, str(root))
                self.assertNotEqual(after, before)
                store.append(old.id, [NewConversationItem(type="message", response_id=str(uuid4()), data=MessageData(role="user", content=[{"type": "input_text", "text": "extra item"}]))])
                self.assertEqual(rejection(refresh._database(uri, str(root))), "session_inventory_mismatch")
            finally:
                store._engine.dispose()

    def test_native_probe_requests_only_full_read_and_rejects_active_turn(self) -> None:
        async def exercise() -> None:
            thread: dict[str, object] = {"id": OLD_THREAD_ID, "cwd": "/fixture", "turns": [{"id": f"past-{index}", "status": "completed", "items": []} for index in range(3)]}
            requests: list[dict[str, object]] = []
            closed: asyncio.Queue[None] = asyncio.Queue()

            async def handler(connection: ServerConnection) -> None:
                try:
                    async for payload in connection:
                        request = json.loads(payload)
                        requests.append(request)
                        if "id" in request:
                            result = {"thread": thread} if request["method"] == "thread/read" else {}
                            await connection.send(json.dumps({"id": request["id"], "result": result}))
                finally:
                    closed.put_nowait(None)

            async with serve(handler, "127.0.0.1", 0) as server:
                url = f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
                self.assertEqual(await refresh._thread(url, "/fixture"), digest(refresh._bytes(thread)))
                await closed.get()
                self.assertEqual(rejection(await refresh._thread(url, "/wrong")), "native_cwd_mismatch")
                await closed.get()
                for turns in ([{"status": "completed"}] * 2, [{"status": "completed"}] * 4, [{"status": "completed"}, {"status": "completed"}, {"status": "inProgress"}], [{"status": "completed"}, {"status": "completed"}, {"status": "failed"}]):
                    thread["turns"] = turns
                    self.assertEqual(rejection(await refresh._thread(url, "/fixture")), "native_not_idle")
                    await closed.get()
            self.assertEqual([request["method"] for request in requests], ["initialize", "initialized", "thread/read"] * 6)
            for request in requests[2::3]:
                self.assertEqual(request["params"], {"threadId": OLD_THREAD_ID, "includeTurns": True})

        asyncio.run(asyncio.wait_for(exercise(), timeout=5))

    def test_null_bridge_and_different_native_cwd_preserve_real_store_and_history(self) -> None:
        async def exercise(root: Path) -> None:
            workspace, native_cwd = root / refresh.WORKSPACE_NAME, root / "native-config"
            workspace.mkdir()
            native_cwd.mkdir()
            request = refresh.RefreshRequest(
                server_pid=os.getpid(), checkout=str(root), published_commit="a" * 40, published_ref="fixture",
                authority_path=str(root / "authority"), task_path=str(root / refresh.TASK_NAME), todo_path=str(root / "TODO"),
                workspace=str(workspace), old_native_cwd=str(native_cwd), old_bridge_cwd=None,
                **{field: str(root / field) for field in ("global_config_path", "fence_database", "control_socket", "preparation_path", "packet_path", "approval_path", "backend_config_path", "journal_path", "log_path")},
            )
            uri = f"sqlite:///{root / 'app.sqlite'}"
            store = SqlAlchemyConversationStore(uri)
            self.addCleanup(store._engine.dispose)
            store.create_conversation(conversation_id=OLD_SESSION_ID, title="old owner", runner_id="runner-old", host_id=OLD_HOST_ID, workspace=str(workspace))
            store.set_external_session_id(OLD_SESSION_ID, OLD_THREAD_ID)
            for index in range(3):
                store.create_conversation(title=f"retained session {index}")
            store.append(OLD_SESSION_ID, [NewConversationItem(type="message", response_id=str(uuid4()), data=MessageData(role="user", content=[{"type": "input_text", "text": f"consumed item {index}"}])) for index in range(22)])
            before_database = refresh._database(uri, str(workspace))
            current = process(os.getpid(), "host")
            assert isinstance(current, Process)
            bridge_path = root / "bridge.json"
            thread: dict[str, object] = {"id": OLD_THREAD_ID, "cwd": str(native_cwd), "turns": [{"id": f"completed-{index}", "status": "completed", "items": [{"consumed": index}]} for index in range(3)]}
            methods: list[str] = []
            drift_during_read = False
            bridge: dict[str, object] = {}

            async def handler(connection: ServerConnection) -> None:
                async for payload in connection:
                    message = json.loads(payload)
                    methods.append(message["method"])
                    if "id" in message:
                        if message["method"] == "thread/read" and drift_during_read:
                            bridge_path.write_bytes(refresh._bytes(bridge | {"cwd": str(workspace)}))
                        result = {"thread": thread} if message["method"] == "thread/read" else {}
                        await connection.send(json.dumps({"id": message["id"], "result": result}))

            async with serve(handler, "127.0.0.1", 0) as server:
                url = f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
                config = BackendConfig(OLD_SESSION_ID, os.getpid(), str(bridge_path), str(root / "host"), "http://127.0.0.1:1")
                Path(request.backend_config_path).write_bytes(refresh._bytes(asdict(config)))
                Path(request.backend_config_path).chmod(0o600)
                bridge = {"session_id": OLD_SESSION_ID, "thread_id": OLD_THREAD_ID, "cwd": None, "active_turn_id": None, "socket_path": url}
                bridge_path.write_bytes(refresh._bytes(bridge))
                with patch.object(refresh, "process_domain", return_value=(current,)), patch.object(refresh, "_public", return_value=digest(b"same public state")):
                    observed = await asyncio.to_thread(refresh.observe, request, uri)
                    assert isinstance(observed, refresh.PreservedState), observed
                    self.assertEqual(observed.native_thread_sha256, digest(refresh._bytes(thread)))
                    self.assertEqual(observed.bridge_sha256, digest(bridge_path.read_bytes()))
                    self.assertEqual(observed, await asyncio.to_thread(refresh.observe, request, uri))
                    wrong_native = replace(request, old_native_cwd=str(workspace))
                    self.assertEqual(rejection(await asyncio.to_thread(refresh.observe, wrong_native, uri)), "native_cwd_mismatch")
                    wrong_bridge = replace(request, old_bridge_cwd=str(native_cwd))
                    self.assertEqual(rejection(await asyncio.to_thread(refresh.observe, wrong_bridge, uri)), "bridge_cwd_mismatch")
                    bridge_path.write_bytes(refresh._bytes(bridge | {"active_turn_id": "active"}))
                    self.assertEqual(rejection(await asyncio.to_thread(refresh.observe, request, uri)), "native_not_idle")
                    bridge_path.write_bytes(refresh._bytes({key: value for key, value in bridge.items() if key != "cwd"}))
                    self.assertEqual(rejection(await asyncio.to_thread(refresh.observe, request, uri)), "bridge_cwd_mismatch")
                    bridge_path.write_bytes(refresh._bytes(bridge))
                    drift_during_read = True
                    self.assertEqual(rejection(await asyncio.to_thread(refresh.observe, request, uri)), "bridge_drift")
            self.assertEqual(methods, ["initialize", "initialized", "thread/read"] * 4)
            self.assertEqual(refresh._database(uri, str(workspace)), before_database)

        with tempfile.TemporaryDirectory(prefix="omnigent-predecessor-binding-") as directory:
            asyncio.run(asyncio.wait_for(exercise(Path(directory).resolve()), timeout=10))


if __name__ == "__main__":
    unittest.main()
