from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "omo_manager" / "omo_manager_setup_watchers.sh"


class WatcherSetupTests(unittest.TestCase):
    def run_setup(
        self,
        tmp: Path,
        *,
        setup: Path = SETUP,
        fake_uv_mode: str = "real",
        email: str = "false",
        health_timeout_s: str = "1",
        email_grace_s: str = "0",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        home = tmp / "home"
        root = tmp / "work_logs"
        state = tmp / "state"
        bin_dir = home / ".config" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        root.mkdir(exist_ok=True)
        state.mkdir(exist_ok=True)
        fake_uv_log = tmp / "fake-uv.log"
        fake_uv = bin_dir / "uv"
        fake_uv.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"${FAKE_UV_LOG:?}"
is_email=0
script=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    run) shift ;;
    --project) shift 2 ;;
    *) script="$1"; shift; break ;;
  esac
done
for arg in "$script" "$@"; do
  [ "${arg##*/}" = "email_idle_watcher.py" ] && is_email=1
done
case "${FAKE_UV_MODE:-real}" in
  real) exec "$script" "$@" ;;
  wrapper) exec python3 -c 'import time; time.sleep(5)' "$script" "$@" ;;
  spoof-argv0) exec -a "${script##*/}" sleep "${FAKE_UV_SLEEP:-30}" ;;
  wrong-child) exec -a not_watcher sleep "${FAKE_UV_SLEEP:-30}" ;;
  email-wrong-child)
    if [ "$is_email" -eq 1 ]; then
      exec -a not_watcher sleep "${FAKE_UV_SLEEP:-30}"
    fi
    exec "$script" "$@"
    ;;
  *) exit 7 ;;
esac
""",
            encoding="utf-8",
        )
        fake_uv.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(home),
            "OMO_MANAGER_LOCAL_ENV": "/dev/null",
            "OMO_MANAGER_TMUX_TARGET": "wl:1",
            "OMO_WORK_LOGS_ROOT": str(root),
            "OMO_MANAGER_STATE_DIR": str(state),
            "OMO_MANAGER_ENABLE_EMAIL_WATCHER": email,
            "OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S": health_timeout_s,
            "OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S": email_grace_s,
            "FAKE_UV_LOG": str(fake_uv_log),
            "FAKE_UV_MODE": fake_uv_mode,
            "FAKE_UV_SLEEP": "5",
        }
        env.update(extra_env or {})
        return subprocess.run([str(setup)], env=env, text=True, capture_output=True, timeout=20, check=False)

    def pid_from_file(self, pid_file: Path) -> int | None:
        try:
            text = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("pid="):
                try:
                    return int(line.removeprefix("pid="))
                except ValueError:
                    return None
        try:
            return int(text)
        except ValueError:
            return None

    def descendant_pids(self, pid: int) -> list[int]:
        result = subprocess.run(["pgrep", "-P", str(pid)], text=True, capture_output=True, check=False)
        pids: list[int] = []
        for line in result.stdout.splitlines():
            try:
                child = int(line)
            except ValueError:
                continue
            pids.append(child)
            pids.extend(self.descendant_pids(child))
        return pids

    def terminate_tree(self, pid: int) -> None:
        pids = [*self.descendant_pids(pid), pid]
        for target in pids:
            try:
                os.kill(target, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue
        time.sleep(0.2)
        for target in pids:
            try:
                os.kill(target, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError:
                continue

    def stop_supervisors(self, state: Path) -> None:
        for pid_file in state.glob("*-supervisor.pid"):
            pid = self.pid_from_file(pid_file)
            if pid is None:
                continue
            self.terminate_tree(pid)

    def process_start_ticks(self, pid: int) -> str:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        rest = stat.rsplit(") ", 1)[1].split()
        return rest[19]

    def process_active(self, pid: int) -> bool:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        state = stat.rsplit(") ", 1)[1].split()[0]
        return state != "Z"

    def wait_for_file(self, path: Path) -> None:
        for _ in range(50):
            if path.exists():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {path}")

    def test_setup_writes_pidfile_and_verifies_child_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("watchers ready", result.stdout)
                self.assertTrue((tmp / "state" / "pending-supervisor.pid").exists())
                pending_pid = self.pid_from_file(tmp / "state" / "pending-supervisor.pid")
                self.assertIsNotNone(pending_pid)
                assert pending_pid is not None
                self.assertEqual(pending_pid, os.getsid(pending_pid))
                self.assertIn("omo_pending_watch.py", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_supervisor_without_watcher_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="wrapper")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not start omo_pending_watch.py", result.stderr)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_spoofed_watcher_argv0(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="spoof-argv0")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not start omo_pending_watch.py", result.stderr)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    def test_pending_failure_cleans_up_email_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="wrong-child", email="true")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not start omo_pending_watch.py", result.stderr)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
                self.assertFalse((tmp / "state" / "email-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_invalid_health_timeout_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_setup(tmp, health_timeout_s="0.5")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S must be a non-negative integer", result.stderr)
            self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())

    def test_setup_rejects_invalid_email_grace_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_setup(tmp, email="true", email_grace_s="bogus")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S must be a non-negative integer", result.stderr)
            self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            self.assertFalse((tmp / "state" / "email-supervisor.pid").exists())

    def test_setup_rejects_invalid_email_mode_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_setup(tmp, email="maybe")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OMO_MANAGER_ENABLE_EMAIL_WATCHER must be auto, true, or false", result.stderr)
            self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())

    def test_empty_inherited_values_do_not_erase_local_routing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            local_env = tmp / "local.env"
            local_root = tmp / "work_logs"
            local_env.write_text(
                f'OMO_WORK_LOGS_ROOT="{local_root}"\nOMO_MANAGER_TMUX_TARGET="wl:1"\n',
                encoding="utf-8",
            )
            try:
                result = self.run_setup(
                    tmp,
                    extra_env={
                        "OMO_MANAGER_LOCAL_ENV": str(local_env),
                        "OMO_WORK_LOGS_ROOT": "",
                        "OMO_MANAGER_TMUX_TARGET": "",
                    },
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("manager_target=wl:1", result.stdout)
                self.assertIn(f"--root {local_root}", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_partial_split_email_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_setup(tmp, extra_env={"OMO_AGENT_GMAIL_ADDRESS": "agent@example.test"})
            self.assertEqual(2, result.returncode)
            self.assertIn("requires OMO_AGENT_GMAIL_ADDRESS", result.stderr)
            self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())

    def test_setup_does_not_kill_unowned_email_watcher_processes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            unrelated = subprocess.Popen(["bash", "-c", "exec -a email_idle_watcher.py sleep 30"])
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(unrelated.poll())
            finally:
                unrelated.terminate()
                try:
                    unrelated.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    unrelated.kill()
                self.stop_supervisors(tmp / "state")

    def test_setup_does_not_kill_direct_matching_root_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            root.mkdir()
            direct = subprocess.Popen(
                ["setsid", str(ROOT / "omo_manager" / "omo_pending_watch.py"), "--root", str(root)],
                env={**os.environ, "OMO_MANAGER_TMUX_TARGET": "wl:1"},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(direct.poll())
            finally:
                try:
                    os.killpg(direct.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                direct.wait(timeout=2)
                self.stop_supervisors(tmp / "state")

    def test_setup_does_not_kill_legacy_supervisor_for_root_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            other_root = tmp / "work_logs-old"
            other_root.mkdir()
            legacy = subprocess.Popen(
                [
                    "setsid",
                    "bash",
                    "-c",
                    "while :; do sleep 5; done",
                    "pending-watch-supervisor",
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(other_root),
                ]
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(legacy.poll())
            finally:
                try:
                    os.killpg(legacy.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                legacy.wait(timeout=2)
                self.stop_supervisors(tmp / "state")

    def test_setup_replaces_legacy_supervisor_for_exact_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            root.mkdir()
            legacy = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done",
                    "pending-watch-supervisor",
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"stopping legacy pending watcher supervisor pid={legacy.pid}", result.stdout)
                legacy.wait(timeout=2)
                self.assertIsNotNone(legacy.returncode)
            finally:
                if legacy.poll() is None:
                    try:
                        os.killpg(legacy.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    legacy.wait(timeout=2)
                self.stop_supervisors(tmp / "state")

    def test_setup_replaces_legacy_supervisor_even_with_stale_pidfile(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            legacy = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done",
                    "pending-watch-supervisor",
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            (state / "pending-supervisor.pid").write_text(
                f"pid={legacy.pid}\nstart={self.process_start_ticks(legacy.pid)}\ntoken=wrong-token\n",
                encoding="utf-8",
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"stopping legacy pending watcher supervisor pid={legacy.pid}", result.stdout)
                legacy.wait(timeout=2)
                self.assertIsNotNone(legacy.returncode)
            finally:
                if legacy.poll() is None:
                    try:
                        os.killpg(legacy.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    legacy.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_kills_reparented_legacy_supervisor_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            child_pid_file = tmp / "legacy-child.pid"
            root.mkdir()
            legacy = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    f"""
(trap "" TERM; while :; do sleep 30; done) &
printf '%s\\n' "$!" >"{child_pid_file}"
while :; do sleep 30; done
""",
                    "pending-watch-supervisor",
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            try:
                self.wait_for_file(child_pid_file)
                child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"stopping legacy pending watcher supervisor pid={legacy.pid}", result.stdout)
                legacy.wait(timeout=2)
                for _ in range(20):
                    if not self.process_active(child_pid):
                        break
                    time.sleep(0.1)
                self.assertFalse(self.process_active(child_pid))
            finally:
                if legacy.poll() is None:
                    try:
                        os.killpg(legacy.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    legacy.wait(timeout=2)
                try:
                    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                    os.kill(child_pid, signal.SIGKILL)
                except (OSError, ValueError):
                    pass
                self.stop_supervisors(tmp / "state")

    def test_setup_does_not_kill_unowned_current_format_pending_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            root.mkdir()
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'owner_token="$1"\nshift\nwhile :; do sleep 5; done',
                    "pending-watch-supervisor",
                    "unowned-token",
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(current.poll())
            finally:
                try:
                    os.killpg(current.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                current.wait(timeout=2)
                self.stop_supervisors(tmp / "state")

    def test_setup_replaces_stale_current_format_pending_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            old_launch_pid_file = state / ".pending-supervisor.oldtoken.pid"
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    str(old_launch_pid_file),
                    "oldtoken",
                    "uv",
                    "run",
                    "--project",
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"stopping legacy pending watcher supervisor pid={current.pid}", result.stdout)
                current.wait(timeout=2)
                self.assertIsNotNone(current.returncode)
            finally:
                if current.poll() is None:
                    try:
                        os.killpg(current.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_replaces_pidfile_supervisor_started_through_symlink_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            alias = tmp / "helper-alias"
            root.mkdir()
            state.mkdir()
            alias.mkdir()
            (alias / "omo_pending_watch.py").symlink_to(ROOT / "omo_manager" / "omo_pending_watch.py")
            token = "alias-token"
            launch_pid_file = state / f".pending-supervisor.{token}.pid"
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    str(launch_pid_file),
                    token,
                    "uv",
                    "run",
                    "--project",
                    str(alias),
                    str(alias / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            (state / "pending-supervisor.pid").write_text(
                f"pid={current.pid}\nstart={self.process_start_ticks(current.pid)}\ntoken={token}\n",
                encoding="utf-8",
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                current.wait(timeout=2)
                self.assertIsNotNone(current.returncode)
                self.assertNotIn("stale pending watcher pidfile points at unowned", result.stderr)
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_replaces_pidfile_supervisor_with_equivalent_root_alias(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            root_alias = tmp / "logs-alias"
            state = tmp / "state"
            root.mkdir()
            root_alias.symlink_to(root)
            state.mkdir()
            token = "root-alias-token"
            launch_pid_file = state / f".pending-supervisor.{token}.pid"
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do read -t 30 || true; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    str(launch_pid_file),
                    token,
                    "uv",
                    "run",
                    "--project",
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            (state / "pending-supervisor.pid").write_text(
                f"pid={current.pid}\nstart={self.process_start_ticks(current.pid)}\ntoken={token}\n",
                encoding="utf-8",
            )
            try:
                result = self.run_setup(tmp, extra_env={"OMO_WORK_LOGS_ROOT": str(root_alias)})
                self.assertEqual(0, result.returncode, result.stderr)
                current.wait(timeout=2)
                self.assertNotIn("stale pending watcher pidfile points at unowned", result.stderr)
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_does_not_kill_legacy_supervisor_started_through_symlink_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            alias = tmp / "helper-alias"
            root.mkdir()
            state.mkdir()
            alias.mkdir()
            (alias / "omo_pending_watch.py").symlink_to(ROOT / "omo_manager" / "omo_pending_watch.py")
            legacy = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    "uv",
                    "run",
                    "--project",
                    str(alias),
                    str(alias / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(legacy.poll())
            finally:
                if legacy.poll() is None:
                    self.terminate_tree(legacy.pid)
                    legacy.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_symlink_entrypoint_refreshes_one_canonical_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            alias = tmp / "helper-alias"
            alias.mkdir()
            alias_setup = alias / "omo_manager_setup_watchers.sh"
            alias_setup.symlink_to(SETUP)
            try:
                first = self.run_setup(tmp, setup=alias_setup)
                self.assertEqual(0, first.returncode, first.stderr)
                first_pid = self.pid_from_file(tmp / "state" / "pending-supervisor.pid")
                self.assertIsNotNone(first_pid)

                second = self.run_setup(tmp)
                self.assertEqual(0, second.returncode, second.stderr)
                second_pid = self.pid_from_file(tmp / "state" / "pending-supervisor.pid")
                self.assertIsNotNone(second_pid)
                self.assertNotEqual(first_pid, second_pid)
                assert first_pid is not None
                self.assertFalse(self.process_active(first_pid))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_does_not_kill_current_format_supervisor_with_mismatched_token(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            launch_pid_file = state / ".pending-supervisor.real-token.pid"
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    str(launch_pid_file),
                    "other-token",
                    "uv",
                    "run",
                    "--project",
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(current.poll())
            finally:
                try:
                    os.killpg(current.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_replaces_legacy_email_supervisor_for_exact_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            legacy = subprocess.Popen(
                [
                    "setsid",
                    "bash",
                    "-c",
                    "while :; do sleep 30; done",
                    "email-watch-supervisor",
                    str(ROOT / "omo_manager" / "email_idle_watcher.py"),
                    "--root",
                    str(root),
                    "--mail-dir",
                    str(root / "manager_mail"),
                    "--state-dir",
                    str(state),
                ]
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"stopping legacy email watcher supervisor pid={legacy.pid}", result.stdout)
                legacy.wait(timeout=2)
                self.assertIsNotNone(legacy.returncode)
            finally:
                if legacy.poll() is None:
                    try:
                        os.killpg(legacy.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    legacy.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_replaces_stale_current_format_email_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            old_launch_pid_file = state / ".email-supervisor.oldtoken.pid"
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done # email watcher exited status",
                    "email-watch-supervisor",
                    str(old_launch_pid_file),
                    "oldtoken",
                    "uv",
                    "run",
                    "--project",
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / "email_idle_watcher.py"),
                    "--root",
                    str(root),
                    "--mail-dir",
                    str(root / "manager_mail"),
                    "--state-dir",
                    str(state),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"stopping legacy email watcher supervisor pid={current.pid}", result.stdout)
                current.wait(timeout=2)
                self.assertIsNotNone(current.returncode)
            finally:
                if current.poll() is None:
                    try:
                        os.killpg(current.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_ignores_stale_pidfile_for_lookalike_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            lookalike = subprocess.Popen(
                [
                    "setsid",
                    "bash",
                    "-c",
                    "while :; do sleep 5; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    "lookalike-token",
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ]
            )
            (state / "pending-supervisor.pid").write_text(f"pid={lookalike.pid}\nstart={self.process_start_ticks(lookalike.pid)}\ntoken=wrong-token\n", encoding="utf-8")
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIsNone(lookalike.poll())
            finally:
                try:
                    os.killpg(lookalike.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                lookalike.wait(timeout=2)
                self.stop_supervisors(state)

    def test_explicit_email_failure_removes_email_pidfile(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="email-wrong-child", email="true")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("email watcher failed to stay running", result.stderr)
                self.assertFalse((tmp / "state" / "email-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    def test_auto_email_failure_removes_email_pidfile_and_keeps_pending(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            home_config = tmp / "home" / ".config" / "himalaya"
            home_config.mkdir(parents=True)
            (home_config / "config.toml").write_text("", encoding="utf-8")
            try:
                result = self.run_setup(tmp, fake_uv_mode="email-wrong-child", email="auto")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("watchers ready", result.stdout)
                self.assertFalse((tmp / "state" / "email-supervisor.pid").exists())
                self.assertTrue((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")


if __name__ == "__main__":
    unittest.main()
