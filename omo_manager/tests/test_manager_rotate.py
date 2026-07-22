import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omo_manager.omo_manager_rotate import (
    Args,
    Coordinator,
    LaunchMetadata,
    PaneIdentity,
    Preflight,
    ProcessInfo,
    RotationError,
    acquire_lock,
    begin_coordinator_handoff,
    cleanup_pre_go_handoff,
    coordinator_rotation,
    create_reservation,
    execute_rotation,
    fresh_command,
    invocation_is_target,
    option_values,
    preflight,
    read_reservation,
    reject_or_clear_stale_reservation,
    resolve_exact_pane,
    rotate,
    select_launch_metadata,
    spawn_coordinator,
    update_reservation,
    wait_for_startup,
)


def completed(command: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def process(pid: int, ppid: int, *argv: str, state: str = "S") -> ProcessInfo:
    return ProcessInfo(pid, ppid, state, tuple(argv))


class ManagerRotateTests(unittest.TestCase):
    def args(
        self,
        root: Path,
        state_dir: Path,
        *,
        model: str | None = None,
        effort: str | None = None,
        coordinator_token: str | None = None,
    ) -> Args:
        return Args("manager:2.0", root, state_dir, model, effort, 2.0, 0.01, coordinator_token)

    def pane(self, cwd: Path) -> PaneIdentity:
        return PaneIdentity("manager:2.0", "%42", "@9", 100, cwd)

    def prepared(self, root: Path, state_dir: Path) -> Preflight:
        return Preflight(
            self.args(root, state_dir),
            self.pane(root),
            LaunchMetadata(
                "gpt-5.6-terra",
                "xhigh",
                "inferred",
                101,
                ("/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra", "--config", 'model_reasoning_effort="xhigh"'),
            ),
            "old manager output\n",
            "worker defaults\n\nmanager instructions\n",
        )

    def test_resolve_requires_exact_full_target_and_rejects_prefix_resolution(self) -> None:
        with self.assertRaisesRegex(RotationError, "numeric tmux target"):
            resolve_exact_pane("manager:two")
        with tempfile.TemporaryDirectory() as tmp:
            result = completed([], stdout=f"manager-long\t2\t0\t%42\t@9\t1\t100\t{tmp}\n")
            with patch("omo_manager.omo_manager_rotate.run", return_value=result):
                with self.assertRaisesRegex(RotationError, "ambiguous or non-exact"):
                    resolve_exact_pane("manager:2.0")

    def test_resolve_returns_exact_pane_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = completed([], stdout=f"manager\t2\t0\t%42\t@9\t1\t100\t{tmp}\n")
            with patch("omo_manager.omo_manager_rotate.run", return_value=result):
                pane = resolve_exact_pane("manager:2.0")
        self.assertEqual("%42", pane.pane_id)
        self.assertEqual("@9", pane.window_id)
        self.assertEqual(100, pane.pane_pid)

    def test_resolve_accepts_window_shorthand_only_for_single_pane_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            single = completed([], stdout=f"wl\t1\t0\t%42\t@9\t1\t100\t{tmp}\n")
            with patch("omo_manager.omo_manager_rotate.run", return_value=single):
                pane = resolve_exact_pane("wl:1")
            self.assertEqual("wl:1.0", pane.canonical_target)

            multiple = completed([], stdout=f"wl\t1\t0\t%42\t@9\t2\t100\t{tmp}\n")
            with patch("omo_manager.omo_manager_rotate.run", return_value=multiple):
                with self.assertRaisesRegex(RotationError, "ambiguous or non-exact"):
                    resolve_exact_pane("wl:1")

    def test_detects_target_invocation_by_pane_or_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = self.pane(root)
            processes = {100: process(100, 1, "zsh"), 200: process(200, 100, "python", "omo_manager_rotate.py")}
            with (
                patch("omo_manager.omo_manager_rotate.os.getpid", return_value=200),
                patch.dict("omo_manager.omo_manager_rotate.os.environ", {"TMUX_PANE": "%different"}),
            ):
                self.assertTrue(invocation_is_target(pane, processes))
            with (
                patch("omo_manager.omo_manager_rotate.os.getpid", return_value=300),
                patch.dict("omo_manager.omo_manager_rotate.os.environ", {"TMUX_PANE": "%42"}),
            ):
                self.assertTrue(invocation_is_target(pane, processes))

    def test_internal_coordinator_refuses_target_pane_to_prevent_recursion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pane = self.pane(root)
            args = self.args(root, root / "state", model="gpt-5.6-terra", effort="xhigh", coordinator_token="a" * 32)
            with (
                patch("omo_manager.omo_manager_rotate.resolve_exact_pane", return_value=pane),
                patch("omo_manager.omo_manager_rotate.read_processes", return_value={100: process(100, 1, "zsh")}),
                patch.dict("omo_manager.omo_manager_rotate.os.environ", {"TMUX_PANE": "%42"}),
            ):
                with self.assertRaisesRegex(RotationError, "coordinator pane must differ"):
                    preflight(args)

    def test_argv_parsing_supports_long_short_and_equals_forms(self) -> None:
        models, efforts = option_values(
            (
                "/bin/bunx",
                "@openai/codex",
                "--dangerously-bypass-approvals-and-sandbox",
                "--model=gpt-5.6-terra",
                "-cmodel_reasoning_effort='ultra'",
                "initial prompt",
            )
        )
        self.assertEqual(["gpt-5.6-terra"], models)
        self.assertEqual(["ultra"], efforts)

    def test_infers_metadata_from_exactly_one_live_launch(self) -> None:
        processes = {
            100: process(100, 1, "zsh"),
            101: process(101, 100, "/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra", "--config", 'model_reasoning_effort="max"'),
            102: process(102, 101, "/bin/node", "/bin/codex"),
        }
        metadata = select_launch_metadata(processes, 100, None, None)
        self.assertEqual(("gpt-5.6-terra", "max", "inferred", 101), (metadata.model, metadata.reasoning_effort, metadata.source, metadata.launch_pid))

    def test_rejects_ambiguous_live_launches_and_conflicting_argv_metadata(self) -> None:
        launch = ("/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra", "--config", 'model_reasoning_effort="high"')
        processes = {100: process(100, 1, "zsh"), 101: process(101, 100, *launch), 102: process(102, 100, *launch)}
        with self.assertRaisesRegex(RotationError, "multiple live"):
            select_launch_metadata(processes, 100, None, None)

        conflicting = {
            100: process(100, 1, "zsh"),
            101: process(101, 100, "/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra", "--model", "gpt-5.7", "--config", 'model_reasoning_effort="high"'),
        }
        with self.assertRaisesRegex(RotationError, "conflicting model"):
            select_launch_metadata(conflicting, 100, None, None)

    def test_overrides_are_required_only_when_inference_is_unavailable(self) -> None:
        no_launch = {100: process(100, 1, "zsh")}
        with self.assertRaisesRegex(RotationError, "both --model and --reasoning-effort"):
            select_launch_metadata(no_launch, 100, None, None)
        metadata = select_launch_metadata(no_launch, 100, "gpt-5.6-terra", "xhigh")
        self.assertEqual("override", metadata.source)

        complete = {
            100: process(100, 1, "zsh"),
            101: process(101, 100, "/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra", "--config", 'model_reasoning_effort="xhigh"'),
        }
        with self.assertRaisesRegex(RotationError, "allowed only"):
            select_launch_metadata(complete, 100, "gpt-5.6-terra", "xhigh")

    def test_partial_inference_requires_matching_override_pair(self) -> None:
        processes = {100: process(100, 1, "zsh"), 101: process(101, 100, "/bin/bunx", "@openai/codex", "--model", "gpt-5.6-terra")}
        metadata = select_launch_metadata(processes, 100, "gpt-5.6-terra", "high")
        self.assertEqual("high", metadata.reasoning_effort)
        with self.assertRaisesRegex(RotationError, "conflicts with inferred model"):
            select_launch_metadata(processes, 100, "gpt-5.7", "high")

    def test_fresh_command_has_explicit_metadata_and_no_resume_or_uuid(self) -> None:
        metadata = LaunchMetadata("gpt-5.6-terra", "xhigh", "override", None, ())
        command = fresh_command(
            metadata,
            Path("/private/state/manager-prompt.txt"),
            "wl:1.0",
            Path("/home/sichangheagent/work_logs"),
            Path("/private/state"),
        )
        self.assertIn("bunx @openai/codex --dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("--model gpt-5.6-terra", command)
        self.assertIn('model_reasoning_effort="xhigh"', command)
        self.assertIn("OMO_AGENT_TMUX_TARGET=wl:1.0", command)
        self.assertIn("OMO_MANAGER_TMUX_TARGET=wl:1.0", command)
        self.assertIn("OMO_MANAGER_STATE_DIR=/private/state", command)
        self.assertIn("OMO_WORK_LOGS_ROOT=/home/sichangheagent/work_logs", command)
        self.assertNotIn("resume", command.casefold())
        self.assertNotRegex(command, r"[0-9a-f]{8}-[0-9a-f-]{27,}")

    def test_self_path_starts_detached_coordinator_with_explicit_args_and_private_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root with spaces; literal"
            root.mkdir()
            state_dir = Path(tmp) / "private state"
            prepared = self.prepared(root, state_dir)
            calls: list[list[str]] = []

            def fake_run(command: list[str], *, timeout: float = 10, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                calls.append(command)
                return completed(command, stdout="%77\n")

            token = "a" * 32
            with patch("omo_manager.omo_manager_rotate.run", side_effect=fake_run):
                coordinator = spawn_coordinator(prepared, token)

            self.assertEqual("%77", coordinator.pane_id)
            self.assertEqual(["tmux", "new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "manager:", "-n", "manager-rotate-coordinator"], calls[0][:-1])
            shell_command = calls[0][-1]
            self.assertIn(f"--_coordinator-token {token}", shell_command)
            self.assertIn("--target manager:2.0", shell_command)
            self.assertIn("--model gpt-5.6-terra", shell_command)
            self.assertIn("--reasoning-effort xhigh", shell_command)
            self.assertIn(shlex.quote(str(root)), shell_command)
            self.assertIn(shlex.quote(str(state_dir)), shell_command)
            self.assertIn(shlex.quote(str(coordinator.log_path)), shell_command)
            self.assertIn('tmux set-option -p -t "$TMUX_PANE" remain-on-exit off', shell_command)
            self.assertEqual(0o600, os.stat(coordinator.log_path).st_mode & 0o777)

    def test_external_path_stays_direct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = self.prepared(root, root / "state")
            audit_path = root / "audit.json"
            with (
                patch("omo_manager.omo_manager_rotate.acquire_lock", return_value=10),
                patch("omo_manager.omo_manager_rotate.os.close"),
                patch("omo_manager.omo_manager_rotate.preflight", return_value=external),
                patch("omo_manager.omo_manager_rotate.execute_rotation", return_value=audit_path) as execute,
                patch("omo_manager.omo_manager_rotate.spawn_coordinator") as spawn,
            ):
                result = rotate(external.args)
            self.assertEqual(audit_path, result.path)
            self.assertFalse(result.coordinated)
            execute.assert_called_once_with(external)
            spawn.assert_not_called()

    def test_coordinator_failure_to_start_is_reported_without_live_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = self.prepared(root, root / "state")
            failed = completed([], returncode=1, stderr="can't create window")
            with patch("omo_manager.omo_manager_rotate.run", return_value=failed):
                with self.assertRaisesRegex(RotationError, "failed to start detached rotation coordinator"):
                    spawn_coordinator(prepared, "a" * 32)

    def test_parent_requires_ready_before_go_and_cleans_pre_go_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = self.prepared(root, root / "state")
            coordinator = Coordinator("%77", "a" * 32, root / "coordinator.log")
            calls = 0

            def handoff(_command: list[str], *, timeout: float = 10.0) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise RotationError("timed out waiting for READY")

            with (
                patch("omo_manager.omo_manager_rotate.secrets.token_hex", return_value="a" * 32),
                patch("omo_manager.omo_manager_rotate.spawn_coordinator", return_value=coordinator),
                patch("omo_manager.omo_manager_rotate.tmux_handoff", side_effect=handoff),
                patch("omo_manager.omo_manager_rotate.run", return_value=completed([])) as run,
            ):
                with self.assertRaisesRegex(RotationError, "READY"):
                    begin_coordinator_handoff(prepared)
            self.assertIsNone(read_reservation(prepared.args.state_dir))
            self.assertIn(["tmux", "kill-pane", "-t", "%77"], [call.args[0] for call in run.call_args_list])

    def test_pre_go_cleanup_clears_reservation_when_kill_pane_times_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            token = "a" * 32
            coordinator = Coordinator("%77", token, state_dir / "coordinator.log")
            create_reservation(state_dir, token)
            update_reservation(state_dir, token, coordinator.pane_id, "started")

            def timed_out(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if "kill-pane" in command:
                    raise subprocess.TimeoutExpired(command, 5)
                return completed(command)

            with patch("omo_manager.omo_manager_rotate.run", side_effect=timed_out):
                cleanup_pre_go_handoff(state_dir, coordinator, token)
            self.assertIsNone(read_reservation(state_dir))

    def test_parent_reserves_channels_before_spawn_and_sends_go_after_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = self.prepared(root, root / "state")
            token = "a" * 32
            coordinator = Coordinator("%77", token, root / "coordinator.log")
            commands: list[list[str]] = []

            def handoff(command: list[str], *, timeout: float = 10.0) -> None:
                commands.append(command)
                if len(commands) == 3:
                    update_reservation(prepared.args.state_dir, token, "%77", "ready")

            with (
                patch("omo_manager.omo_manager_rotate.secrets.token_hex", return_value=token),
                patch("omo_manager.omo_manager_rotate.spawn_coordinator", return_value=coordinator) as spawn,
                patch("omo_manager.omo_manager_rotate.tmux_handoff", side_effect=handoff),
            ):
                self.assertEqual(coordinator, begin_coordinator_handoff(prepared))
            spawn.assert_called_once_with(prepared, token)
            self.assertEqual("-L", commands[0][2])
            self.assertEqual("-L", commands[1][2])
            self.assertIn("ready", commands[2][-1])
            self.assertEqual("-U", commands[-1][2])
            self.assertIn("go", commands[-1][-1])

    def test_active_reservation_rejected_and_stale_missing_pane_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            create_reservation(state_dir, "a" * 32)
            update_reservation(state_dir, "a" * 32, "%77", "ready")
            with patch("omo_manager.omo_manager_rotate.pane_exists", return_value=True):
                with self.assertRaisesRegex(RotationError, "active manager rotation handoff"):
                    acquire_lock(state_dir)
            with patch("omo_manager.omo_manager_rotate.pane_exists", return_value=False):
                reject_or_clear_stale_reservation(state_dir)
            self.assertIsNone(read_reservation(state_dir))

    def test_stale_pre_pane_reservation_clears_only_when_main_lock_is_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            create_reservation(state_dir, "a" * 32)
            with patch("omo_manager.omo_manager_rotate.rotation_lock_is_free", return_value=False):
                with self.assertRaisesRegex(RotationError, "active manager rotation handoff"):
                    reject_or_clear_stale_reservation(state_dir)
            with patch("omo_manager.omo_manager_rotate.rotation_lock_is_free", return_value=True):
                reject_or_clear_stale_reservation(state_dir)
            self.assertIsNone(read_reservation(state_dir))

    def test_matching_coordinator_clears_reservation_only_after_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            token = "a" * 32
            args = self.args(root, state_dir, model="gpt-5.6-terra", effort="xhigh", coordinator_token=token)
            create_reservation(state_dir, token)
            update_reservation(state_dir, token, "%77", "started")
            pane = self.pane(root)
            prepared = self.prepared(root, state_dir)
            events: list[str] = []

            def locked(*_args: object, **_kwargs: object) -> int:
                events.append("lock")
                return 12

            def execute(_prepared: Preflight) -> Path:
                self.assertIsNone(read_reservation(state_dir))
                events.append("execute")
                return root / "audit.json"

            with (
                patch.dict("omo_manager.omo_manager_rotate.os.environ", {"TMUX_PANE": "%77"}),
                patch("omo_manager.omo_manager_rotate.resolve_exact_pane", return_value=pane),
                patch("omo_manager.omo_manager_rotate.tmux_handoff"),
                patch("omo_manager.omo_manager_rotate.acquire_lock", side_effect=locked),
                patch("omo_manager.omo_manager_rotate.preflight", return_value=prepared),
                patch("omo_manager.omo_manager_rotate.execute_rotation", side_effect=execute),
                patch("omo_manager.omo_manager_rotate.os.close"),
            ):
                coordinator_rotation(args)
            self.assertEqual(["lock", "execute"], events)

    def test_coordinator_go_timeout_leaves_reservation_for_stale_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            token = "a" * 32
            args = self.args(root, state_dir, model="gpt-5.6-terra", effort="xhigh", coordinator_token=token)
            create_reservation(state_dir, token)
            update_reservation(state_dir, token, "%77", "started")
            with (
                patch.dict("omo_manager.omo_manager_rotate.os.environ", {"TMUX_PANE": "%77"}),
                patch("omo_manager.omo_manager_rotate.resolve_exact_pane", return_value=self.pane(root)),
                patch("omo_manager.omo_manager_rotate.tmux_handoff", side_effect=[None, RotationError("GO timeout")]),
                patch("omo_manager.omo_manager_rotate.execute_rotation") as execute,
            ):
                with self.assertRaisesRegex(RotationError, "GO timeout"):
                    coordinator_rotation(args)
            self.assertEqual("ready", read_reservation(state_dir).phase)  # type: ignore[union-attr]
            execute.assert_not_called()

    def test_execute_respawns_same_pane_then_starts_watchers_with_explicit_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            prepared = self.prepared(root, state_dir)
            events: list[str] = []
            calls: list[tuple[list[str], dict[str, str] | None]] = []

            def fake_run(command: list[str], *, timeout: float = 10, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                calls.append((command, env))
                events.append("watcher" if command[0].endswith("omo_manager_setup_watchers.sh") else "respawn")
                return completed(command)

            with (
                patch("omo_manager.omo_manager_rotate.run", side_effect=fake_run),
                patch("omo_manager.omo_manager_rotate.verify_same_pane", side_effect=lambda *_: events.append("verify")),
                patch("omo_manager.omo_manager_rotate.wait_for_startup", side_effect=lambda *_: events.append("status") or "running"),
            ):
                audit_path = execute_rotation(prepared)

            self.assertEqual(["respawn", "verify", "status", "verify", "watcher"], events)
            respawn, _ = calls[0]
            self.assertEqual(["tmux", "respawn-pane", "-k", "-t", "%42", "-c", str(root)], respawn[:7])
            self.assertNotIn("resume", respawn[-1].casefold())
            watcher_env = calls[-1][1]
            assert watcher_env is not None
            self.assertEqual(str(root), watcher_env["OMO_WORK_LOGS_ROOT"])
            self.assertEqual("manager:2.0", watcher_env["OMO_MANAGER_TMUX_TARGET"])
            record = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("succeeded", record["outcome"])
            self.assertEqual("old manager output\n", record["prior_pane_output"])
            self.assertEqual("gpt-5.6-terra", record["launch"]["model"])

    def test_startup_polls_through_transient_not_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = self.prepared(root, root / "state")
            with (
                patch("omo_manager.omo_manager_rotate.time.monotonic", side_effect=[0.0, 0.1, 0.2, 0.3]),
                patch("omo_manager.omo_manager_rotate.time.sleep"),
                patch("omo_manager.omo_manager_rotate.verify_same_pane"),
                patch("omo_manager.omo_manager_rotate.status_classification", side_effect=["not_codex", "running"]),
            ):
                self.assertEqual("running", wait_for_startup(prepared))

    def test_startup_error_fails_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = self.prepared(root, root / "state")
            with (
                patch("omo_manager.omo_manager_rotate.time.monotonic", side_effect=[0.0, 0.1]),
                patch("omo_manager.omo_manager_rotate.time.sleep"),
                patch("omo_manager.omo_manager_rotate.verify_same_pane"),
                patch("omo_manager.omo_manager_rotate.status_classification", return_value="error"),
            ):
                with self.assertRaisesRegex(RotationError, "classified as error"):
                    wait_for_startup(prepared)

    def test_startup_failure_never_runs_watchers_and_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prepared = self.prepared(root, root / "state")
            commands: list[list[str]] = []

            def fake_run(command: list[str], *, timeout: float = 10, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return completed(command)

            with (
                patch("omo_manager.omo_manager_rotate.run", side_effect=fake_run),
                patch("omo_manager.omo_manager_rotate.verify_same_pane"),
                patch("omo_manager.omo_manager_rotate.wait_for_startup", side_effect=RotationError("startup error")),
            ):
                with self.assertRaisesRegex(RotationError, "startup error"):
                    execute_rotation(prepared)
            self.assertEqual(1, len(commands))
            self.assertEqual("respawn-pane", commands[0][1])
            audit_path = next((root / "state" / "rotations").glob("manager-rotation-*.json"))
            self.assertEqual("failed", json.loads(audit_path.read_text(encoding="utf-8"))["outcome"])


if __name__ == "__main__":
    unittest.main()
