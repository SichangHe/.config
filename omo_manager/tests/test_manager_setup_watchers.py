from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "omo_manager" / "omo_manager_setup_watchers.sh"
TEST_MANAGER_TARGET = "omo-watcher-test:1"
TMUX = shutil.which("tmux")
SLEEP = shutil.which("sleep")
FLOCK = shutil.which("flock")
READLINK = shutil.which("readlink")
SED = shutil.which("sed")
SETSID = shutil.which("setsid")
CHMOD = shutil.which("chmod")


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
        timeout_s: float = 20,
    ) -> subprocess.CompletedProcess[str]:
        home = tmp / "home"
        root = tmp / "work_logs"
        state = tmp / "state"
        bin_dir = home / ".config" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        root.mkdir(exist_ok=True)
        state.mkdir(exist_ok=True)
        fake_uv_log = tmp / "fake-uv.log"
        fake_sleep = bin_dir / "sleep"
        fake_sleep.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_UV_MODE:-real}" = root-swap-ready || "${FAKE_UV_MODE:-real}" = stopped-ready || "${FAKE_UV_MODE:-real}" = ancestor-loss-ready || "${FAKE_UV_MODE:-real}" = real-root-replaced-ready ]] \
  && [ "${1:-}" = 0.2 ] \
  && mkdir "${FAKE_SLEEP_BARRIER:?}.claim" 2>/dev/null; then
  printf 'waiting\n' >"${FAKE_SLEEP_BARRIER}.waiting"
  while [ ! -e "${FAKE_SLEEP_BARRIER}.released" ]; do
    "${REAL_SLEEP:?}" 0.01
  done
  exit 0
fi
exec "${REAL_SLEEP:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_sleep.chmod(0o755)
        fake_flock = bin_dir / "flock"
        fake_flock.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "${FAKE_UV_MODE:-real}" = pre-teardown-root-swap ] \
  && [ "${1:-}" = -n ] \
  && [ "${2:-}" = 8 ] \
  && mkdir "${FAKE_FLOCK_BARRIER:?}.claim" 2>/dev/null; then
  mv "${FAKE_PREFLIGHT_ROOT:?}" "${FAKE_PREFLIGHT_ROOT}.moved"
  ln -s "${FAKE_PREFLIGHT_ROOT}.moved" "${FAKE_PREFLIGHT_ROOT}"
fi
exec "${REAL_FLOCK:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_flock.chmod(0o755)
        fake_readlink = bin_dir / "readlink"
        fake_readlink.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "${FAKE_UV_MODE:-real}" = final-trailing-symlink ] \
  && [ "${3:-}" = "${FAKE_FINAL_ROOT:-}" ] \
  && mkdir "${FAKE_READLINK_BARRIER:?}.claim" 2>/dev/null; then
  mv "${FAKE_FINAL_ROOT_LEXICAL:?}" "${FAKE_FINAL_ROOT_LEXICAL}.moved"
  ln -s "${FAKE_FINAL_ROOT_LEXICAL}.moved" "${FAKE_FINAL_ROOT_LEXICAL}"
fi
if [ "${FAKE_UV_MODE:-real}" = final-auth-root-swap ] \
  && [ "${1:-}" = -f ] \
  && [ "${2:-}" = -- ] \
  && [ "${3:-}" = "${FAKE_TEARDOWN_ROOT:-}" ]; then
  readlink_count=0
  if [ -r "${FAKE_READLINK_COUNT:?}" ]; then
    read -r readlink_count <"${FAKE_READLINK_COUNT}"
  fi
  readlink_count=$((readlink_count + 1))
  printf '%s\n' "$readlink_count" >"${FAKE_READLINK_COUNT}"
  if [ "$readlink_count" -eq 3 ] \
    && mkdir "${FAKE_READLINK_BARRIER:?}.claim" 2>/dev/null; then
    mv "${FAKE_TEARDOWN_ROOT}" "${FAKE_TEARDOWN_ROOT}.moved"
    mkdir "${FAKE_TEARDOWN_ROOT}"
  fi
fi
exec "${REAL_READLINK:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_readlink.chmod(0o755)
        fake_sed = bin_dir / "sed"
        fake_sed.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_UV_MODE:-real}" = teardown-real-root-swap || "${FAKE_UV_MODE:-real}" = dead-pidfile-root-swap || "${FAKE_UV_MODE:-real}" = stale-pidfile-root-swap ]] \
  && [ "${3:-}" = "${FAKE_CURRENT_PID_FILE:-}" ] \
  && mkdir "${FAKE_SED_BARRIER:?}.claim" 2>/dev/null; then
  mv "${FAKE_TEARDOWN_ROOT:?}" "${FAKE_TEARDOWN_ROOT}.moved"
  mkdir "${FAKE_TEARDOWN_ROOT}"
fi
exec "${REAL_SED:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_sed.chmod(0o755)
        fake_setsid = bin_dir / "setsid"
        fake_setsid.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${FAKE_UV_MODE:-real}" = launch-timeout || "${FAKE_UV_MODE:-real}" = launch-timeout-root-swap ]]; then
  launch_pid_file=""
  for arg in "$@"; do
    case "$arg" in
      */.pending-supervisor.*.pid) launch_pid_file="$arg"; break ;;
    esac
  done
  [ -n "$launch_pid_file" ] || exit 13
  : >"$launch_pid_file"
  printf '%s\n' "$$" >"${FAKE_TIMEOUT_LAUNCHER_PID:?}"
  if [ "${FAKE_UV_MODE:-real}" = launch-timeout-root-swap ]; then
    mv "${FAKE_TEARDOWN_ROOT:?}" "${FAKE_TEARDOWN_ROOT}.moved"
    mkdir "${FAKE_TEARDOWN_ROOT}"
  fi
  exec "${REAL_SETSID:?}" bash -c 'while :; do sleep 30; done' launch-timeout-watch-supervisor
fi
exec "${REAL_SETSID:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_setsid.chmod(0o755)
        fake_chmod = bin_dir / "chmod"
        fake_chmod.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
if [ "${FAKE_UV_MODE:-real}" = post-pidfile-guardian-exit ] \
  && [ "${2:-}" = "${FAKE_STABLE_PID_FILE:-}" ] \
  && mkdir "${FAKE_CHMOD_BARRIER:?}.claim" 2>/dev/null; then
  supervisor_pid="$(sed -n 's/^pid=//p' "$2" | sed -n '1p')"
  kill -TERM "$supervisor_pid"
  for (( attempt=0; attempt<200; attempt++ )); do
    if [ ! -r "/proc/$supervisor_pid/stat" ]; then
      break
    fi
    supervisor_stat="$(<"/proc/$supervisor_pid/stat")"
    supervisor_rest="${supervisor_stat##*) }"
    [ "${supervisor_rest%% *}" = Z ] && break
    "${REAL_SLEEP:?}" 0.01
  done
fi
exec "${REAL_CHMOD:?}" "$@"
""",
            encoding="utf-8",
        )
        fake_chmod.chmod(0o755)
        fake_uv = bin_dir / "uv"
        # 🧑 "If harness fake watcher exits too early, make it realistically stay alive through acceptance and prove cleanup."
        fake_uv.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'OMO_MANAGER_TMUX_TARGET=%s\\n' "${OMO_MANAGER_TMUX_TARGET-}" >>"${FAKE_UV_LOG:?}"
printf 'FAKE_UV_PID=%s\\n' "$$" >>"${FAKE_UV_LOG:?}"
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
  wrapper) exec python3 -c 'import signal; signal.pause()' "$script" "$@" ;;
  spoof-argv0) exec -a "${script##*/}" sleep infinity ;;
  wrong-child) exec -a not_watcher sleep infinity ;;
  root-swap-ready|stopped-ready|ancestor-loss-ready|real-root-replaced-ready)
    root_arg=""
    ready_file=""
    next_arg=""
    for arg in "$@"; do
      case "$next_arg" in
        root) root_arg="$arg"; next_arg=""; continue ;;
        ready) ready_file="$arg"; next_arg=""; continue ;;
      esac
      case "$arg" in
        --root) next_arg=root ;;
        --ready-file) next_arg=ready ;;
      esac
    done
    while [ ! -e "${FAKE_SLEEP_BARRIER:?}.waiting" ]; do
      "${REAL_SLEEP:?}" 0.01
    done
    root_arg_lexical="$root_arg"
    while [ "$root_arg_lexical" != / ] && [[ "$root_arg_lexical" == */ ]]; do
      root_arg_lexical="${root_arg_lexical%/}"
    done
    if [ "${FAKE_UV_MODE:-real}" = real-root-replaced-ready ]; then
      mv "$root_arg_lexical" "$root_arg_lexical.moved"
      mkdir "$root_arg_lexical"
    fi
    "$script" "$@" &
    watcher_pid=$!
    while [ ! -s "$ready_file" ]; do
      if ! kill -0 "$watcher_pid" 2>/dev/null; then
        wait "$watcher_pid"
        exit 8
      fi
      "${REAL_SLEEP:?}" 0.01
    done
    ready_pid="$(sed -n 's/^pid=//p' "$ready_file" | sed -n '1p')"
    [ "$ready_pid" = "$watcher_pid" ] || exit 9
    cp "$ready_file" "${FAKE_READY_SNAPSHOT:?}"
    if [ "${FAKE_UV_MODE:-real}" = stopped-ready ]; then
      kill -STOP "$watcher_pid"
      watcher_stat="$(<"/proc/$watcher_pid/stat")"
      watcher_rest="${watcher_stat##*) }"
      [ "${watcher_rest%% *}" = T ] || exit 10
    fi
    printf 'AUTHENTIC_WATCHER_PID=%s\n' "$watcher_pid" >>"${FAKE_UV_LOG:?}"
    if [ "${FAKE_UV_MODE:-real}" = root-swap-ready ]; then
      mv "$root_arg_lexical" "$root_arg_lexical.moved"
      ln -s "$root_arg_lexical.moved" "$root_arg_lexical"
    elif [ "${FAKE_UV_MODE:-real}" = ancestor-loss-ready ]; then
      root_parent="$(dirname "$root_arg")"
      [ -L "$root_parent" ] || exit 11
      rm "$root_parent"
    fi
    printf 'released\n' >"${FAKE_SLEEP_BARRIER}.released"
    wait "$watcher_pid"
    ;;
  mismatched-root-ready|mismatched-ready-file)
    root_arg=""
    ready_file=""
    next_arg=""
    for arg in "$@"; do
      case "$next_arg" in
        root) root_arg="$arg"; next_arg=""; continue ;;
        ready) ready_file="$arg"; next_arg=""; continue ;;
      esac
      case "$arg" in
        --root) next_arg=root ;;
        --ready-file) next_arg=ready ;;
      esac
    done
    launched_root="$root_arg"
    launched_ready_file="$ready_file"
    if [ "${FAKE_UV_MODE:-real}" = mismatched-root-ready ]; then
      launched_root="${FAKE_WRONG_ROOT:?}"
    else
      launched_ready_file="$ready_file.other"
    fi
    "$script" --root "$launched_root" --ready-file "$launched_ready_file" &
    watcher_pid=$!
    while [ ! -s "$launched_ready_file" ]; do
      if ! kill -0 "$watcher_pid" 2>/dev/null; then
        wait "$watcher_pid"
        exit 12
      fi
      "${REAL_SLEEP:?}" 0.01
    done
    root_dev="$(stat -Lc '%d' "$root_arg")"
    root_ino="$(stat -Lc '%i' "$root_arg")"
    umask 077
    printf 'version=omo-pending-watch-ready-v1\npid=%s\nroot=%s\nroot_dev=%s\nroot_ino=%s\n' \
      "$watcher_pid" "$root_arg" "$root_dev" "$root_ino" >"$ready_file"
    cp "$ready_file" "${FAKE_READY_SNAPSHOT:?}"
    printf 'AUTHENTIC_WATCHER_PID=%s\n' "$watcher_pid" >>"${FAKE_UV_LOG:?}"
    wait "$watcher_pid"
    ;;
  late-fork-failure|late-setsid-failure)
    late_fork() {
      python3 -c 'import os, pathlib, signal, sys; os.setsid() if sys.argv[2].startswith("late-setsid") else os.setpgrp(); pathlib.Path(sys.argv[1]).write_text(f"{os.getpid()} {os.getpgrp()} {os.getsid(0)}\\n", encoding="utf-8"); signal.pause()' "${FAKE_LATE_INFO:?}" "${FAKE_UV_MODE}" &
      while [ ! -s "${FAKE_LATE_INFO}" ]; do
        "${REAL_SLEEP:?}" 0.01
      done
      read -r late_pid late_pgid late_sid <"${FAKE_LATE_INFO}"
      printf 'LATE_FORK_PID=%s\nLATE_FORK_PGID=%s\nLATE_FORK_SID=%s\n' "$late_pid" "$late_pgid" "$late_sid" >>"${FAKE_UV_LOG:?}"
      exit 0
    }
    trap late_fork TERM
    while :; do
      "${REAL_SLEEP:?}" 1
    done
    ;;
  email-wrong-child|email-health-root-swap)
    if [ "$is_email" -eq 1 ]; then
      if [ "${FAKE_UV_MODE:-real}" = email-health-root-swap ] \
        && mkdir "${FAKE_HEALTH_SWAP_BARRIER:?}.claim" 2>/dev/null; then
        mv "${FAKE_TEARDOWN_ROOT:?}" "${FAKE_TEARDOWN_ROOT}.moved"
        mkdir "${FAKE_TEARDOWN_ROOT}"
      fi
      exec -a not_watcher sleep infinity
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
            "EMAIL_ME_FAKE_SEND_LOG": str(tmp / "email-send.log"),
            "HOME": str(home),
            "TMUX": "",
            "OMO_AGENT_GMAIL_ADDRESS": "",
            "OMO_AGENT_GMAIL_APP_PASSWORD": "",
            "OMO_AGENT_TMUX_TARGET": "omo-watcher-test:0",
            "OMO_HUMAN_EMAIL_ADDRESS": "",
            "OMO_HUMAN_EMAIL_CONFIG_PATH": str(tmp / "missing-email-config.toml"),
            "OMO_MANAGER_LOCAL_ENV": "/dev/null",
            "OMO_MANAGER_TMUX_TARGET": TEST_MANAGER_TARGET,
            "OMO_MANAGER_URL": "",
            "OMO_WORK_LOGS_ROOT": str(root),
            "OMO_MANAGER_STATE_DIR": str(state),
            "OMO_MANAGER_ENABLE_EMAIL_WATCHER": email,
            "OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER": "false",
            "OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S": health_timeout_s,
            "OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S": email_grace_s,
            "FAKE_UV_LOG": str(fake_uv_log),
            "FAKE_UV_MODE": fake_uv_mode,
            "FAKE_SLEEP_BARRIER": str(tmp / "fake-sleep-barrier"),
            "FAKE_READY_SNAPSHOT": str(tmp / "fake-ready-snapshot"),
            "FAKE_FLOCK_BARRIER": str(tmp / "fake-flock-barrier"),
            "FAKE_PREFLIGHT_ROOT": str(root),
            "FAKE_READLINK_BARRIER": str(tmp / "fake-readlink-barrier"),
            "FAKE_READLINK_COUNT": str(tmp / "fake-readlink-count"),
            "FAKE_SED_BARRIER": str(tmp / "fake-sed-barrier"),
            "FAKE_HEALTH_SWAP_BARRIER": str(tmp / "fake-health-swap-barrier"),
            "FAKE_CHMOD_BARRIER": str(tmp / "fake-chmod-barrier"),
            "FAKE_FINAL_ROOT": str(root),
            "FAKE_FINAL_ROOT_LEXICAL": str(root),
            "FAKE_TEARDOWN_ROOT": str(root),
            "FAKE_CURRENT_PID_FILE": str(state / "pending-supervisor.pid"),
            "FAKE_LATE_INFO": str(tmp / "fake-late-info"),
            "FAKE_TIMEOUT_LAUNCHER_PID": str(tmp / "fake-timeout-launcher-pid"),
            "FAKE_STABLE_PID_FILE": str(state / "pending-supervisor.pid"),
            "FAKE_WRONG_ROOT": str(tmp / "wrong-work-logs"),
            "REAL_FLOCK": str(FLOCK),
            "REAL_READLINK": str(READLINK),
            "REAL_SED": str(SED),
            "REAL_SETSID": str(SETSID),
            "REAL_CHMOD": str(CHMOD),
            "REAL_SLEEP": str(SLEEP),
        }
        env.update(extra_env or {})
        return subprocess.run([str(setup)], env=env, text=True, capture_output=True, timeout=timeout_s, check=False)

    def start_tmux_server(self, tmp: Path) -> tuple[Path, int]:
        socket = tmp / "tmux.sock"
        subprocess.run(
            [TMUX, "-S", str(socket), "new-session", "-d", "-s", "watcher-test"],
            text=True,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            [TMUX, "-S", str(socket), "display-message", "-p", "#{pid}"],
            text=True,
            capture_output=True,
            check=True,
        )
        return socket, int(result.stdout.strip())

    def process_parent_id(self, pid: int) -> int:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat.rsplit(") ", 1)[1].split()[1])

    def process_has_ancestor(self, pid: int, ancestor: int) -> bool:
        while pid not in (0, 1):
            pid = self.process_parent_id(pid)
            if pid == ancestor:
                return True
        return False

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

    def supervisor_pidfile_contents(
        self, pid: int, token: str, root: Path, *, containment: str = ""
    ) -> str:
        root_stat = root.stat()
        contents = (
            f"pid={pid}\n"
            f"start={self.process_start_ticks(pid)}\n"
            f"token={token}\n"
            f"root_dev={root_stat.st_dev}\n"
            f"root_ino={root_stat.st_ino}\n"
        )
        if containment:
            contents += f"containment={containment}\n"
        return contents

    def start_owned_pending_supervisor(self, root: Path, state: Path, token: str) -> subprocess.Popen[str]:
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
                str(ROOT / "omo_manager"),
                str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                "--root",
                str(root),
            ],
            start_new_session=True,
            text=True,
        )
        (state / "pending-supervisor.pid").write_text(
            self.supervisor_pidfile_contents(current.pid, token, root),
            encoding="utf-8",
        )
        return current

    def process_active(self, pid: int) -> bool:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            return False
        state = stat.rsplit(") ", 1)[1].split()[0]
        return state != "Z"

    def fake_uv_pids(self, tmp: Path) -> list[int]:
        return [int(match.group(1)) for line in (tmp / "fake-uv.log").read_text(encoding="utf-8").splitlines() if (match := re.fullmatch(r"FAKE_UV_PID=([0-9]+)", line)) is not None]

    def authentic_watcher_pids(self, tmp: Path) -> list[int]:
        return [int(match.group(1)) for line in (tmp / "fake-uv.log").read_text(encoding="utf-8").splitlines() if (match := re.fullmatch(r"AUTHENTIC_WATCHER_PID=([0-9]+)", line)) is not None]

    def late_fork_pids(self, tmp: Path) -> list[int]:
        return [int(match.group(1)) for line in (tmp / "fake-uv.log").read_text(encoding="utf-8").splitlines() if (match := re.fullmatch(r"LATE_FORK_PID=([0-9]+)", line)) is not None]

    def wait_for_process_exit(self, pid: int) -> None:
        for _ in range(50):
            if not Path(f"/proc/{pid}").exists():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for process {pid} to exit")

    def wait_for_file(self, path: Path) -> None:
        for _ in range(50):
            if path.exists():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {path}")

    def test_setup_writes_pidfile_and_verifies_child_watcher(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            watcher_pid: int | None = None
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("watchers ready", result.stdout)
                self.assertTrue((tmp / "state" / "pending-supervisor.pid").exists())
                pidfile = tmp / "state" / "pending-supervisor.pid"
                pidfile_text = pidfile.read_text(encoding="utf-8")
                self.assertIn("containment=subreaper-v1\n", pidfile_text)
                root_stat = (tmp / "work_logs").stat()
                self.assertIn(f"root_dev={root_stat.st_dev}\n", pidfile_text)
                self.assertIn(f"root_ino={root_stat.st_ino}\n", pidfile_text)
                pending_pid = self.pid_from_file(pidfile)
                self.assertIsNotNone(pending_pid)
                assert pending_pid is not None
                self.assertEqual(pending_pid, os.getsid(pending_pid))
                self.assertIn("omo_pending_watch.py", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
                self.assertEqual(1, len(fake_uv_pids := self.fake_uv_pids(tmp)))
                watcher_pid = fake_uv_pids[0]
                self.assertTrue(self.process_active(watcher_pid))
            finally:
                self.stop_supervisors(tmp / "state")
                if watcher_pid is not None:
                    self.wait_for_process_exit(watcher_pid)

    def test_agent_audit_is_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertFalse((tmp / "state" / "audit-supervisor.pid").exists())
                self.assertIn("skipped agent audit watcher", result.stdout)
            finally:
                self.stop_supervisors(tmp / "state")

    def test_agent_audit_supervisor_is_opt_in_and_owned(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, extra_env={"OMO_MANAGER_ENABLE_AGENT_AUDIT": "true"})
                self.assertEqual(0, result.returncode, result.stderr)
                pidfile = tmp / "state" / "audit-supervisor.pid"
                self.assertTrue(pidfile.exists())
                audit_pid = self.pid_from_file(pidfile)
                self.assertIsNotNone(audit_pid)
                assert audit_pid is not None
                self.assertTrue(self.process_active(audit_pid))
                invocation = (tmp / "fake-uv.log").read_text(encoding="utf-8")
                self.assertIn("omo_agent_audit.py", invocation)
                self.assertIn("--loop", invocation)
                self.assertIn("--enable", invocation)
            finally:
                self.stop_supervisors(tmp / "state")

    def test_agent_audit_rejects_invalid_enable_value_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_setup(tmp, extra_env={"OMO_MANAGER_ENABLE_AGENT_AUDIT": "maybe"})
            self.assertEqual(2, result.returncode)
            self.assertIn("OMO_MANAGER_ENABLE_AGENT_AUDIT must be true or false", result.stderr)
            self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())

    def test_local_env_manager_target_overrides_stale_inherited_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            local_env = tmp / "local.env"
            local_env.write_text(
                f"""
export OMO_WORK_LOGS_ROOT="{tmp / "work_logs"}"
export OMO_MANAGER_STATE_DIR="{tmp / "state"}"
export OMO_MANAGER_TMUX_TARGET="wl:1"
export OMO_MANAGER_URL=""
export OMO_MANAGER_ENABLE_EMAIL_WATCHER="false"
""".lstrip(),
                encoding="utf-8",
            )
            try:
                result = self.run_setup(
                    tmp,
                    extra_env={
                        "OMO_MANAGER_LOCAL_ENV": str(local_env),
                        "OMO_MANAGER_TMUX_TARGET": "vlportfolio:32.0",
                    },
                    timeout_s=60,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("manager_target=wl:1", result.stdout)
                self.assertIn("OMO_MANAGER_TMUX_TARGET=wl:1", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(tmp / "state")

    @unittest.skipUnless(TMUX, "tmux required")
    def test_tmux_invocation_hands_setup_to_persistent_server(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            socket, server_pid = self.start_tmux_server(tmp)
            tmux_log = tmp / "tmux-wrapper.log"
            tmux_wrapper = tmp / "home" / ".config" / "bin" / "tmux"
            tmux_wrapper.parent.mkdir(parents=True)
            tmux_wrapper.write_text(
                """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"${FAKE_TMUX_LOG:?}"
exec "${REAL_TMUX:?}" "$@"
""",
                encoding="utf-8",
            )
            tmux_wrapper.chmod(0o755)
            try:
                result = self.run_setup(
                    tmp,
                    timeout_s=40,
                    extra_env={
                        "TMUX": f"{socket},{server_pid},0",
                        "FAKE_TMUX_LOG": str(tmux_log),
                        "REAL_TMUX": TMUX or "",
                    },
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("watchers ready", result.stdout)
                self.assertTrue(
                    any(line.startswith("run-shell -b ") for line in tmux_log.read_text(encoding="utf-8").splitlines())
                )
                pending_pid = self.pid_from_file(tmp / "state" / "pending-supervisor.pid")
                self.assertIsNotNone(pending_pid)
                assert pending_pid is not None
                time.sleep(1)
                self.assertTrue(self.process_active(pending_pid))
                self.assertTrue(
                    self.process_parent_id(pending_pid) == 1
                    or self.process_has_ancestor(pending_pid, server_pid)
                )
            finally:
                self.stop_supervisors(tmp / "state")
                subprocess.run([TMUX, "-S", str(socket), "kill-server"], capture_output=True, check=False)

    @unittest.skipUnless(TMUX, "tmux required")
    def test_tmux_invocation_returns_setup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            socket, server_pid = self.start_tmux_server(tmp)
            try:
                result = self.run_setup(
                    tmp,
                    health_timeout_s="invalid",
                    extra_env={"TMUX": f"{socket},{server_pid},0"},
                )
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S must be a non-negative integer", result.stderr)
            finally:
                subprocess.run([TMUX, "-S", str(socket), "kill-server"], capture_output=True, check=False)

    def test_tmux_handoff_times_out_when_bridge_never_reports_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  run-shell) : ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            started = time.monotonic()
            result = self.run_setup(
                tmp,
                extra_env={
                    "TMUX": "/tmp/fake-tmux,12345,0",
                    "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": "1",
                },
            )
            elapsed = time.monotonic() - started
            self.assertEqual(1, result.returncode)
            self.assertIn("timed out waiting for tmux watcher setup after 1s", result.stderr)
            self.assertLess(elapsed, 5)
            match = re.search(r"handoff state=queued path=(\S+)", result.stderr)
            self.assertIsNotNone(match, result.stderr)
            assert match is not None
            handoff = Path(match.group(1))
            try:
                self.assertTrue(handoff.is_dir())
                self.assertFalse((handoff / "environment").exists())
            finally:
                shutil.rmtree(handoff, ignore_errors=True)

    def test_tmux_timeout_preserves_handoff_until_bridge_publishes_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  run-shell) (sleep 2; bash -c "$3") </dev/null >/dev/null 2>&1 & ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            result = self.run_setup(
                tmp,
                health_timeout_s="invalid",
                extra_env={
                    "TMUX": "/tmp/fake-tmux,12345,0",
                    "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": "1",
                },
            )
            self.assertEqual(1, result.returncode)
            match = re.search(r"handoff state=(?:queued|running) path=(\S+)", result.stderr)
            self.assertIsNotNone(match, result.stderr)
            assert match is not None
            handoff = Path(match.group(1))
            try:
                self.assertTrue(handoff.is_dir())
                self.assertIn((handoff / "state").read_text(encoding="utf-8").strip(), {"queued", "running"})
                self.wait_for_file(handoff / "status")
                self.assertEqual("failed", (handoff / "state").read_text(encoding="utf-8").strip())
                self.assertEqual("125", (handoff / "status").read_text(encoding="utf-8").strip())
                self.assertFalse((handoff / "environment").exists())
            finally:
                shutil.rmtree(handoff, ignore_errors=True)

    def test_tmux_timeout_does_not_cancel_bridge_that_loaded_environment(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            local_env = tmp / "local.env"
            local_env.write_text("sleep 2\n", encoding="utf-8")
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  run-shell) bash -c "$3" </dev/null >/dev/null 2>&1 & ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            result = self.run_setup(
                tmp,
                health_timeout_s="invalid",
                extra_env={
                    "TMUX": "/tmp/fake-tmux,12345,0",
                    "OMO_MANAGER_LOCAL_ENV": str(local_env),
                    "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": "1",
                },
            )
            self.assertEqual(1, result.returncode)
            match = re.search(r"handoff state=running path=(\S+)", result.stderr)
            self.assertIsNotNone(match, result.stderr)
            assert match is not None
            handoff = Path(match.group(1))
            try:
                self.assertFalse((handoff / "environment").exists())
                self.wait_for_file(handoff / "status")
                self.assertEqual("complete", (handoff / "state").read_text(encoding="utf-8").strip())
                self.assertEqual("2", (handoff / "status").read_text(encoding="utf-8").strip())
            finally:
                shutil.rmtree(handoff, ignore_errors=True)

    def test_invalid_tmux_timeout_cleans_temporary_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bridge_tmp = tmp / "bridge-tmp"
            bridge_tmp.mkdir()
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            result = self.run_setup(
                tmp,
                extra_env={
                    "TMUX": "/tmp/fake-tmux,12345,0",
                    "TMPDIR": str(bridge_tmp),
                    "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": "invalid",
                },
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S must be a positive integer", result.stderr)
            self.assertEqual([], list(bridge_tmp.iterdir()))

    def test_tmux_timeout_accepts_leading_zero_decimal_digits(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  run-shell) bash -c "$3" || : ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            for timeout in ("08", "09"):
                with self.subTest(timeout=timeout):
                    result = self.run_setup(
                        tmp,
                        health_timeout_s="invalid",
                        extra_env={
                            "TMUX": "/tmp/fake-tmux,12345,0",
                            "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": timeout,
                        },
                    )
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn(
                        "OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S must be a non-negative integer",
                        result.stderr,
                    )

    def test_tmux_timeout_rejects_leading_zero_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            result = self.run_setup(
                tmp,
                extra_env={
                    "TMUX": "/tmp/fake-tmux,12345,0",
                    "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": "00",
                },
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S must be a positive integer", result.stderr)

    def test_tmux_timeout_treats_010_as_ten_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
case "$1" in
  display-message) printf '12345\\n' ;;
  run-shell) (sleep 9; bash -c "$3") </dev/null >/dev/null 2>&1 & ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            result = self.run_setup(
                tmp,
                health_timeout_s="invalid",
                extra_env={
                    "TMUX": "/tmp/fake-tmux,12345,0",
                    "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S": "010",
                },
            )
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("timed out waiting for tmux watcher setup", result.stderr)

    def test_tmux_handoff_keeps_environment_secrets_out_of_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            bin_dir = tmp / "home" / ".config" / "bin"
            bin_dir.mkdir(parents=True)
            tmux_log = tmp / "fake-tmux.log"
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/usr/bin/env bash
printf '%s\\0' "$@" >>"${FAKE_TMUX_LOG:?}"
printf '\\n' >>"${FAKE_TMUX_LOG:?}"
case "$1" in
  display-message) printf '12345\\n' ;;
  run-shell) bash -c "$3" || : ;;
  wait-for) : ;;
  *) exit 2 ;;
esac
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)
            secret = "tmux-argv-secret-value"
            try:
                result = self.run_setup(
                    tmp,
                    extra_env={
                        "TMUX": "/tmp/fake-tmux,12345,0",
                        "FAKE_TMUX_LOG": str(tmux_log),
                        "OMO_AGENT_GMAIL_APP_PASSWORD": secret,
                        "OMO_MANAGER_TMUX_REENTRY": "0",
                    },
                )
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("split email setup requires", result.stderr)
                arguments = tmux_log.read_bytes().replace(b"\0", b" ")
                self.assertNotIn(secret.encode(), arguments)
                self.assertEqual(1, arguments.count(b"run-shell"))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_supervisor_without_watcher_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="wrapper")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    # 🧑 "Bind readiness to exact live-process `--root ROOT` and token-specific `--ready-file FILE` argv pairs, rejecting same-script descendants with either mismatch."
    def test_setup_rejects_authentic_watcher_with_mismatched_ready_argv(self) -> None:
        for mode in ("mismatched-root-ready", "mismatched-ready-file"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                (tmp / "wrong-work-logs").mkdir()
                try:
                    result = self.run_setup(tmp, fake_uv_mode=mode, health_timeout_s="1")
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("pending watcher did not become ready", result.stderr)
                    self.assertEqual(1, len(watcher_pids := self.authentic_watcher_pids(tmp)))
                    watcher_pid = watcher_pids[0]
                    ready_record = (tmp / "fake-ready-snapshot").read_text(encoding="utf-8")
                    self.assertIn(f"pid={watcher_pid}\n", ready_record)
                    self.assertIn(f"root={tmp / 'work_logs'}\n", ready_record)
                    self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
                    self.wait_for_process_exit(watcher_pid)
                finally:
                    self.stop_supervisors(tmp / "state")

    def test_readiness_failure_cleans_late_fork_reparented_from_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="late-fork-failure", health_timeout_s="1")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertTrue(late_pids := self.late_fork_pids(tmp))
                fake_uv_log = (tmp / "fake-uv.log").read_text(encoding="utf-8")
                late_pgid = int(re.search(r"^LATE_FORK_PGID=([0-9]+)$", fake_uv_log, re.MULTILINE).group(1))
                late_sid = int(re.search(r"^LATE_FORK_SID=([0-9]+)$", fake_uv_log, re.MULTILINE).group(1))
                self.assertNotEqual(late_pgid, late_sid)
                for late_pid in late_pids:
                    self.wait_for_process_exit(late_pid)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")
                for late_pid in self.late_fork_pids(tmp):
                    if self.process_active(late_pid):
                        os.kill(late_pid, signal.SIGKILL)

    def test_readiness_failure_cleans_late_setsid_fork_via_subreaper(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="late-setsid-failure", health_timeout_s="1")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertTrue(late_pids := self.late_fork_pids(tmp))
                fake_uv_log = (tmp / "fake-uv.log").read_text(encoding="utf-8")
                late_pgid = int(re.search(r"^LATE_FORK_PGID=([0-9]+)$", fake_uv_log, re.MULTILINE).group(1))
                late_sid = int(re.search(r"^LATE_FORK_SID=([0-9]+)$", fake_uv_log, re.MULTILINE).group(1))
                self.assertEqual(late_pids[0], late_pgid)
                self.assertEqual(late_pids[0], late_sid)
                for late_pid in late_pids:
                    self.wait_for_process_exit(late_pid)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")
                for late_pid in self.late_fork_pids(tmp):
                    if self.process_active(late_pid):
                        os.kill(late_pid, signal.SIGKILL)

    def test_duplicate_pending_watcher_exit_is_not_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            setup_text = SETUP.read_text(encoding="utf-8")
            match = re.search(r"bash -c '\n(.*?)\n' pending-watch-supervisor", setup_text, re.DOTALL)
            self.assertIsNotNone(match)
            assert match is not None
            calls = tmp / "calls"
            root = tmp / "work_logs"
            root.mkdir()
            root_stat = root.stat()
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    match.group(1),
                    "pending-watch-supervisor",
                    str(tmp / "launch.pid"),
                    "token",
                    f"{root_stat.st_dev}:{root_stat.st_ino}",
                    str(root),
                    "bash",
                    "-c",
                    f"printf x >>{shlex.quote(str(calls))}; exit 75",
                ],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            self.assertEqual(75, result.returncode)
            self.assertEqual("x", calls.read_text(encoding="utf-8"))
            self.assertIn("duplicate-root refusal; stopping supervisor", result.stderr)
            self.assertNotIn("restarting", result.stderr)

    def test_setup_rejects_spoofed_watcher_argv0(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="spoof-argv0")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    def test_pending_failure_cleans_up_email_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="wrong-child", email="true")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
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

    def test_guest_hees_email_watcher_is_disabled_by_default(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn('guest_hees_email_enable="${OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER:-false}"', text)
        self.assertIn("skipped guest-hees email watcher; approval-gated and disabled by default", text)

    def test_setup_rejects_invalid_guest_hees_email_mode_before_launching(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            result = self.run_setup(tmp, extra_env={"OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER": "maybe"})
            self.assertNotEqual(0, result.returncode)
            self.assertIn("OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER must be true or false", result.stderr)
            self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())

    def test_setup_prepares_pinned_guest_hees_watcher(self) -> None:
        text = SETUP.read_text(encoding="utf-8")
        self.assertIn('guest_hees_email_args=(--guest-hees', text)
        self.assertNotIn('--manager-file guest_hees_mail_mgr.md --manager-target guest_hees:0', text)
        self.assertIn('guest_hees_mail_dir="$root/guest_hees_manager_mail"', text)
        guest_block = text.split("guest_hees_email_args=", 1)[1].split("' guest-hees-email-watch-supervisor", 1)[0]
        self.assertIn('startup_grace_s="${OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S:-2}"', guest_block)
        self.assertIn('if [ "$started" -eq 0 ] && [ "$runtime_s" -lt "$startup_grace_s" ]; then', guest_block)
        self.assertIn('guest_hees_email_lock="$state_dir/source1269-guest-watcher.lock"', guest_block)
        self.assertIn('umask 077', guest_block)
        self.assertIn('flock --exclusive --nonblock --conflict-exit-code 75 "$lock_file" "$@"', guest_block)
        self.assertIn('if [ "$st" -eq 75 ]; then', guest_block)

    def test_empty_inherited_values_do_not_erase_local_routing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            local_env = tmp / "local.env"
            local_root = tmp / "work_logs"
            local_env.write_text(
                f'OMO_WORK_LOGS_ROOT="{local_root}"\nOMO_MANAGER_TMUX_TARGET="{TEST_MANAGER_TARGET}"\n',
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
                self.assertIn(f"manager_target={TEST_MANAGER_TARGET}", result.stdout)
                self.assertIn(f"--root {local_root}", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_inherited_root_that_conflicts_with_local_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            local_root = tmp / "configured-work-logs"
            inherited_root = tmp / "stale-work-logs"
            local_root.mkdir()
            inherited_root.mkdir()
            local_env = tmp / "local.env"
            local_env.write_text(f'OMO_WORK_LOGS_ROOT="{local_root}"\n', encoding="utf-8")
            state = tmp / "state"
            email: subprocess.Popen[bytes] | None = None
            try:
                started = self.run_setup(
                    tmp,
                    extra_env={
                        "OMO_MANAGER_LOCAL_ENV": str(local_env),
                        "OMO_WORK_LOGS_ROOT": str(local_root),
                    },
                )
                self.assertEqual(0, started.returncode, started.stderr)
                pidfile = state / "pending-supervisor.pid"
                before = pidfile.read_text(encoding="utf-8")
                pid = int(before.splitlines()[0].removeprefix("pid="))
                email_token = "retained-email"
                email_launch_pidfile = state / f".email-supervisor.{email_token}.pid"
                email = subprocess.Popen(
                    [
                        "bash",
                        "-c",
                        "while :; do read -t 30 || true; done # email watcher exited status",
                        "email-watch-supervisor",
                        str(email_launch_pidfile),
                        email_token,
                        "uv",
                        "run",
                        "--project",
                        str(ROOT / "omo_manager"),
                        str(ROOT / "omo_manager" / "email_idle_watcher.py"),
                        "--root",
                        str(local_root),
                        "--mail-dir",
                        str(local_root / "manager_mail"),
                        "--state-dir",
                        str(state),
                    ],
                    start_new_session=True,
                )
                email_pidfile = state / "email-supervisor.pid"
                email_pidfile.write_text(
                    self.supervisor_pidfile_contents(email.pid, email_token, local_root),
                    encoding="utf-8",
                )
                email_before = email_pidfile.read_text(encoding="utf-8")

                result = self.run_setup(
                    tmp,
                    extra_env={
                        "OMO_MANAGER_LOCAL_ENV": str(local_env),
                        "OMO_WORK_LOGS_ROOT": str(inherited_root),
                    },
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("refusing to replace configured watchers", result.stderr)
                self.assertEqual(before, pidfile.read_text(encoding="utf-8"))
                self.assertEqual(email_before, email_pidfile.read_text(encoding="utf-8"))
                self.assertTrue(self.process_active(pid))
                self.assertTrue(self.process_active(email.pid))
            finally:
                self.stop_supervisors(state)
                if email is not None:
                    email.wait(timeout=2)

    def test_setup_uses_inherited_root_when_local_configuration_omits_it(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            inherited_root = tmp / "inherited-work-logs"
            inherited_root.mkdir()
            local_env = tmp / "local.env"
            local_env.write_text(f'OMO_MANAGER_TMUX_TARGET="{TEST_MANAGER_TARGET}"\n', encoding="utf-8")
            try:
                result = self.run_setup(
                    tmp,
                    extra_env={
                        "OMO_MANAGER_LOCAL_ENV": str(local_env),
                        "OMO_WORK_LOGS_ROOT": str(inherited_root),
                    },
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"--root {inherited_root}", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_accepts_real_root_beneath_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            trusted = tmp / "trusted"
            actual = trusted / "actual"
            alias = trusted / "alias"
            root = alias / "work_logs"
            (actual / "work_logs").mkdir(parents=True)
            alias.symlink_to(actual, target_is_directory=True)
            try:
                result = self.run_setup(tmp, extra_env={"OMO_WORK_LOGS_ROOT": str(root)})
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"--root {root}", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_replaces_supervisor_through_equivalent_symlinked_ancestor_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            trusted = tmp / "trusted"
            actual = trusted / "actual"
            alias = trusted / "alias"
            real_root = actual / "work_logs"
            alias_root = alias / "work_logs"
            state = tmp / "state"
            real_root.mkdir(parents=True)
            alias.symlink_to(actual, target_is_directory=True)
            state.mkdir()
            current = self.start_owned_pending_supervisor(real_root, state, "ancestor-alias-root")
            try:
                result = self.run_setup(tmp, extra_env={"OMO_WORK_LOGS_ROOT": str(alias_root)})
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                current.wait(timeout=2)
                replacement_pid = self.pid_from_file(state / "pending-supervisor.pid")
                self.assertIsNotNone(replacement_pid)
                self.assertNotEqual(current.pid, replacement_pid)
                self.assertIn(f"--root {alias_root}", (tmp / "fake-uv.log").read_text(encoding="utf-8"))
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_rejects_inherited_symlink_root_before_starting_watchers(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            local_root = tmp / "configured-work-logs"
            root_alias = tmp / "work-logs-alias"
            local_root.mkdir()
            root_alias.symlink_to(local_root)
            local_env = tmp / "local.env"
            local_env.write_text(f'OMO_WORK_LOGS_ROOT="{local_root}"\n', encoding="utf-8")
            try:
                result = self.run_setup(
                    tmp,
                    extra_env={
                        "OMO_MANAGER_LOCAL_ENV": str(local_env),
                        "OMO_WORK_LOGS_ROOT": str(root_alias),
                    },
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("must be a real directory, not a symlink", result.stderr)
                self.assertFalse((tmp / "fake-uv.log").exists())
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_root_symlink_created_after_authentic_ready_publication(self) -> None:
        for trailing_slash in (False, True):
            with self.subTest(trailing_slash=trailing_slash), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                root = tmp / "work_logs"
                root_arg = f"{root}/" if trailing_slash else str(root)
                try:
                    result = self.run_setup(
                        tmp,
                        fake_uv_mode="root-swap-ready",
                        health_timeout_s="3",
                        extra_env={"OMO_WORK_LOGS_ROOT": root_arg},
                    )
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("pending watcher did not become ready", result.stderr)
                    self.assertEqual(1, len(watcher_pids := self.authentic_watcher_pids(tmp)))
                    watcher_pid = watcher_pids[0]
                    ready_record = (tmp / "fake-ready-snapshot").read_text(encoding="utf-8")
                    self.assertIn("version=omo-pending-watch-ready-v1\n", ready_record)
                    self.assertIn(f"pid={watcher_pid}\n", ready_record)
                    self.assertIn(f"root={root}\n", ready_record)
                    self.assertTrue(root.is_symlink())
                    self.wait_for_process_exit(watcher_pid)
                finally:
                    self.stop_supervisors(tmp / "state")

    def test_setup_rejects_real_root_replacement_after_teardown_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            moved_root = tmp / "work_logs.moved"
            try:
                result = self.run_setup(tmp, fake_uv_mode="real-root-replaced-ready", health_timeout_s="3")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertEqual(1, len(watcher_pids := self.authentic_watcher_pids(tmp)))
                watcher_pid = watcher_pids[0]
                ready_record = (tmp / "fake-ready-snapshot").read_text(encoding="utf-8")
                self.assertIn(f"pid={watcher_pid}\n", ready_record)
                self.assertIn(f"root={root}\n", ready_record)
                self.assertTrue(root.is_dir())
                self.assertTrue(moved_root.is_dir())
                self.assertIn(f"root_ino={root.stat().st_ino}\n", ready_record)
                self.assertNotEqual(root.stat().st_ino, moved_root.stat().st_ino)
                self.wait_for_process_exit(watcher_pid)
            finally:
                self.stop_supervisors(tmp / "state")

    def test_pending_supervisor_does_not_restart_after_post_readiness_root_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            supervisor_pid: int | None = None
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                pidfile = state / "pending-supervisor.pid"
                pidfile_contents = pidfile.read_text(encoding="utf-8")
                supervisor_pid = self.pid_from_file(pidfile)
                self.assertIsNotNone(supervisor_pid)
                self.assertEqual(1, len(self.fake_uv_pids(tmp)))
                root.rename(tmp / "work_logs.moved")
                root.mkdir()
                assert supervisor_pid is not None
                self.wait_for_process_exit(supervisor_pid)
                time.sleep(0.2)
                self.assertEqual(1, len(self.fake_uv_pids(tmp)))
                self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                self.assertIn("pending watcher root identity changed; stopping supervisor", (state / "pending-watch.log").read_text(encoding="utf-8"))
            finally:
                self.stop_supervisors(state)
                if supervisor_pid is not None and self.process_active(supervisor_pid):
                    self.terminate_tree(supervisor_pid)

    def test_setup_rejects_stopped_authentic_watcher_after_ready_publication(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            try:
                result = self.run_setup(tmp, fake_uv_mode="stopped-ready", health_timeout_s="3")
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertEqual(1, len(watcher_pids := self.authentic_watcher_pids(tmp)))
                watcher_pid = watcher_pids[0]
                ready_record = (tmp / "fake-ready-snapshot").read_text(encoding="utf-8")
                self.assertIn("version=omo-pending-watch-ready-v1\n", ready_record)
                self.assertIn(f"pid={watcher_pid}\n", ready_record)
                self.assertIn(f"root={tmp / 'work_logs'}\n", ready_record)
                self.assertFalse((tmp / "work_logs").is_symlink())
                self.wait_for_process_exit(watcher_pid)
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_cleans_authentic_watcher_after_root_ancestor_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            trusted = tmp / "trusted"
            actual = trusted / "actual"
            alias = trusted / "alias"
            root = alias / "work_logs"
            (actual / "work_logs").mkdir(parents=True)
            alias.symlink_to(actual, target_is_directory=True)
            try:
                result = self.run_setup(
                    tmp,
                    fake_uv_mode="ancestor-loss-ready",
                    health_timeout_s="3",
                    extra_env={"OMO_WORK_LOGS_ROOT": str(root)},
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("pending watcher did not become ready", result.stderr)
                self.assertEqual(1, len(watcher_pids := self.authentic_watcher_pids(tmp)))
                watcher_pid = watcher_pids[0]
                ready_record = (tmp / "fake-ready-snapshot").read_text(encoding="utf-8")
                self.assertIn(f"pid={watcher_pid}\n", ready_record)
                self.assertIn(f"root={root}\n", ready_record)
                self.assertFalse(alias.exists())
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
                self.wait_for_process_exit(watcher_pid)
            finally:
                self.stop_supervisors(tmp / "state")

    def test_setup_rejects_invalid_root_before_stopping_current_supervisor(self) -> None:
        for kind in ("missing", "file", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                previous_root = tmp / "work_logs"
                state = tmp / "state"
                invalid_root = tmp / f"{kind}-root"
                previous_root.mkdir()
                state.mkdir()
                if kind == "file":
                    invalid_root.write_text("not a directory\n", encoding="utf-8")
                elif kind == "symlink":
                    invalid_root.symlink_to(previous_root, target_is_directory=True)
                token = f"invalid-root-{kind}"
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
                        str(ROOT / "omo_manager"),
                        str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                        "--root",
                        str(previous_root),
                    ],
                    start_new_session=True,
                )
                (state / "pending-supervisor.pid").write_text(
                    self.supervisor_pidfile_contents(current.pid, token, previous_root),
                    encoding="utf-8",
                )
                try:
                    result = self.run_setup(tmp, extra_env={"OMO_WORK_LOGS_ROOT": str(invalid_root)})
                    self.assertEqual(2, result.returncode)
                    expected_error = "must be a real directory, not a symlink" if kind == "symlink" else "must be an existing directory"
                    self.assertIn(expected_error, result.stderr)
                    self.assertIsNone(current.poll())
                    self.assertFalse((tmp / "fake-uv.log").exists())
                finally:
                    if current.poll() is None:
                        self.terminate_tree(current.pid)
                        current.wait(timeout=2)

    # 🧑 "Add realistic inherited/configured/final trailing-slash symlink cases proving the current authenticated supervisor remains alive and unchanged."
    def test_setup_rejects_trailing_slash_symlink_at_each_root_preflight(self) -> None:
        for source in ("inherited", "configured", "final"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                previous_root = tmp / "work_logs"
                state = tmp / "state"
                previous_root.mkdir()
                state.mkdir()
                token = f"trailing-symlink-{source}"
                current = self.start_owned_pending_supervisor(previous_root, state, token)
                pidfile = state / "pending-supervisor.pid"
                pidfile_contents = pidfile.read_text(encoding="utf-8")
                local_env = tmp / "local.env"
                fake_uv_mode = "real"
                extra_env: dict[str, str] = {"OMO_MANAGER_LOCAL_ENV": str(local_env)}
                if source == "final":
                    final_root = tmp / "final-work-logs"
                    final_root.mkdir()
                    alias_parent = tmp / "alias-parent"
                    alias_parent.symlink_to(tmp, target_is_directory=True)
                    inherited_root = f"{final_root}/"
                    configured_root = alias_parent / final_root.name
                    local_env.write_text(f'OMO_WORK_LOGS_ROOT="{configured_root}"\n', encoding="utf-8")
                    extra_env.update(
                        {
                            "OMO_WORK_LOGS_ROOT": inherited_root,
                            "FAKE_FINAL_ROOT": inherited_root,
                            "FAKE_FINAL_ROOT_LEXICAL": str(final_root),
                        }
                    )
                    fake_uv_mode = "final-trailing-symlink"
                else:
                    target = tmp / "symlink-target"
                    symlink_root = tmp / "symlink-root"
                    target.mkdir()
                    symlink_root.symlink_to(target, target_is_directory=True)
                    trailing_symlink = f"{symlink_root}/"
                    if source == "inherited":
                        local_env.write_text('OMO_WORK_LOGS_ROOT=""\n', encoding="utf-8")
                        extra_env["OMO_WORK_LOGS_ROOT"] = trailing_symlink
                    else:
                        local_env.write_text(f'OMO_WORK_LOGS_ROOT="{trailing_symlink}"\n', encoding="utf-8")
                        extra_env["OMO_WORK_LOGS_ROOT"] = str(previous_root)
                try:
                    result = self.run_setup(tmp, fake_uv_mode=fake_uv_mode, extra_env=extra_env)
                    self.assertEqual(2, result.returncode)
                    self.assertIn("must be a real directory, not a symlink", result.stderr)
                    self.assertIsNone(current.poll())
                    self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                    self.assertFalse((tmp / "fake-uv.log").exists())
                finally:
                    if current.poll() is None:
                        self.terminate_tree(current.pid)
                        current.wait(timeout=2)

    def test_setup_rejects_terminal_dot_after_symlink_root_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            current = self.start_owned_pending_supervisor(root, state, "terminal-dot-symlink")
            pidfile = state / "pending-supervisor.pid"
            pidfile_contents = pidfile.read_text(encoding="utf-8")
            alias = tmp / "root-alias"
            alias.symlink_to(root, target_is_directory=True)
            try:
                result = self.run_setup(tmp, extra_env={"OMO_WORK_LOGS_ROOT": f"{alias}/."})
                self.assertEqual(2, result.returncode)
                self.assertIn("must be a real directory, not a symlink", result.stderr)
                self.assertIsNone(current.poll())
                self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                self.assertFalse((tmp / "fake-uv.log").exists())
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)

    def test_setup_revalidates_root_identity_before_stopping_current_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            token = "pre-teardown-root-swap"
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
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(root),
                ],
                start_new_session=True,
            )
            (state / "pending-supervisor.pid").write_text(
                self.supervisor_pidfile_contents(current.pid, token, root),
                encoding="utf-8",
            )
            try:
                result = self.run_setup(tmp, fake_uv_mode="pre-teardown-root-swap")
                self.assertEqual(2, result.returncode)
                self.assertIn("must be a real directory, not a symlink", result.stderr)
                self.assertIsNone(current.poll())
                self.assertFalse((tmp / "fake-uv.log").exists())
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)

    def test_setup_revalidates_root_identity_inside_current_supervisor_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            current = self.start_owned_pending_supervisor(root, state, "teardown-real-root-swap")
            pidfile = state / "pending-supervisor.pid"
            pidfile_contents = pidfile.read_text(encoding="utf-8")
            try:
                result = self.run_setup(tmp, fake_uv_mode="teardown-real-root-swap")
                self.assertEqual(2, result.returncode)
                self.assertIn("work-log root changed before watcher teardown", result.stderr)
                self.assertIsNone(current.poll())
                self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                self.assertTrue(root.is_dir())
                self.assertTrue((tmp / "work_logs.moved").is_dir())
                self.assertFalse((tmp / "fake-uv.log").exists())
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)

    def test_setup_revalidates_root_after_final_process_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            current = self.start_owned_pending_supervisor(root, state, "final-auth-root-swap")
            pidfile = state / "pending-supervisor.pid"
            pidfile_contents = pidfile.read_text(encoding="utf-8")
            try:
                result = self.run_setup(tmp, fake_uv_mode="final-auth-root-swap")
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("work-log root changed before watcher teardown", result.stderr)
                self.assertIsNone(current.poll())
                self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                self.assertTrue(root.is_dir())
                self.assertTrue((tmp / "work_logs.moved").is_dir())
                self.assertFalse((tmp / "fake-uv.log").exists())
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)

    def test_setup_revalidates_root_before_removing_dead_or_stale_pidfile(self) -> None:
        for kind in ("dead", "stale"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                root = tmp / "work_logs"
                state = tmp / "state"
                root.mkdir()
                state.mkdir()
                stale_process: subprocess.Popen[str] | None = None
                if kind == "dead":
                    pid = 999_999_999
                    start = "1"
                else:
                    stale_process = subprocess.Popen(["sleep", "30"], text=True)
                    pid = stale_process.pid
                    start = self.process_start_ticks(pid)
                pidfile = state / "pending-supervisor.pid"
                pidfile_contents = f"pid={pid}\nstart={start}\ntoken={'a' * 32}\n"
                pidfile.write_text(pidfile_contents, encoding="utf-8")
                try:
                    result = self.run_setup(tmp, fake_uv_mode=f"{kind}-pidfile-root-swap")
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("work-log root changed before watcher teardown", result.stderr)
                    self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                    self.assertTrue((tmp / "work_logs.moved").is_dir())
                    self.assertFalse((tmp / "fake-uv.log").exists())
                    if stale_process is not None:
                        self.assertIsNone(stale_process.poll())
                finally:
                    if stale_process is not None and stale_process.poll() is None:
                        stale_process.terminate()
                        stale_process.wait(timeout=2)

    def test_launch_report_timeout_preserves_process_and_record_after_root_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            launcher_pid: int | None = None
            try:
                result = self.run_setup(tmp, fake_uv_mode="launch-timeout-root-swap")
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("work-log root changed before watcher teardown", result.stderr)
                launcher_pid = int((tmp / "fake-timeout-launcher-pid").read_text(encoding="utf-8"))
                self.assertTrue(self.process_active(launcher_pid))
                launch_records = list((tmp / "state").glob(".pending-supervisor.*.pid"))
                self.assertEqual(1, len(launch_records))
                self.assertTrue(launch_records[0].exists())
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
                self.assertTrue((tmp / "work_logs.moved").is_dir())
            finally:
                if launcher_pid is not None and self.process_active(launcher_pid):
                    self.terminate_tree(launcher_pid)

    def test_launch_report_timeout_preserves_process_and_record_with_stable_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            launcher_pid: int | None = None
            try:
                result = self.run_setup(tmp, fake_uv_mode="launch-timeout")
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn(
                    "pending watcher supervisor did not report an authenticated pid; retaining launch record and process",
                    result.stderr,
                )
                launcher_pid = int((tmp / "fake-timeout-launcher-pid").read_text(encoding="utf-8"))
                self.assertTrue(self.process_active(launcher_pid))
                launch_records = list((tmp / "state").glob(".pending-supervisor.*.pid"))
                self.assertEqual(1, len(launch_records))
                self.assertTrue(launch_records[0].exists())
                self.assertFalse((tmp / "state" / "pending-supervisor.pid").exists())
                self.assertTrue((tmp / "work_logs").is_dir())
                self.assertFalse((tmp / "work_logs.moved").exists())
            finally:
                if launcher_pid is not None and self.process_active(launcher_pid):
                    self.terminate_tree(launcher_pid)

    def test_launch_identity_is_rechecked_before_launch_record_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            state = tmp / "state"
            supervisor_pid: int | None = None
            try:
                result = self.run_setup(tmp, fake_uv_mode="post-pidfile-guardian-exit")
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn(
                    "pending watcher supervisor identity changed before launch record deletion; retaining recovery records",
                    result.stderr,
                )
                pidfile = state / "pending-supervisor.pid"
                self.assertTrue(pidfile.exists())
                supervisor_pid = self.pid_from_file(pidfile)
                self.assertIsNotNone(supervisor_pid)
                launch_records = list(state.glob(".pending-supervisor.*.pid"))
                self.assertEqual(1, len(launch_records))
                self.assertEqual(f"{supervisor_pid}\n", launch_records[0].read_text(encoding="utf-8"))
                assert supervisor_pid is not None
                self.wait_for_process_exit(supervisor_pid)
            finally:
                self.stop_supervisors(state)
                if supervisor_pid is not None and self.process_active(supervisor_pid):
                    self.terminate_tree(supervisor_pid)

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
                env={
                    **os.environ,
                    "EMAIL_ME_FAKE_SEND_LOG": str(tmp / "email-send.log"),
                    "OMO_AGENT_GMAIL_ADDRESS": "",
                    "OMO_AGENT_GMAIL_APP_PASSWORD": "",
                    "OMO_HUMAN_EMAIL_ADDRESS": "",
                    "OMO_HUMAN_EMAIL_CONFIG_PATH": str(tmp / "missing-email-config.toml"),
                    "OMO_MANAGER_LOCAL_ENV": str(tmp / "missing-local.env"),
                    "OMO_MANAGER_STATE_DIR": str(tmp / "direct-state"),
                    "OMO_MANAGER_TMUX_TARGET": TEST_MANAGER_TARGET,
                    "OMO_MANAGER_URL": "",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                result = self.run_setup(tmp)
                self.assertNotEqual(0, result.returncode)
                self.assertRegex(
                    result.stderr,
                    r"pending watcher (?:supervisor exited|supervisor identity changed before launch record deletion)",
                )
                self.assertIsNone(direct.poll())
                pidfile = tmp / "state" / "pending-supervisor.pid"
                if "identity changed before launch record deletion" in result.stderr:
                    self.assertTrue(pidfile.exists())
                    self.assertEqual(1, len(list((tmp / "state").glob(".pending-supervisor.*.pid"))))
                else:
                    self.assertFalse(pidfile.exists())
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
                self.supervisor_pidfile_contents(current.pid, token, root),
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
                self.assertEqual(2, result.returncode)
                self.assertIn("must be a real directory, not a symlink", result.stderr)
                self.assertIsNone(current.poll())
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_preserves_pidfile_supervisors_with_a_different_root(self) -> None:
        for name, script_name in (
            ("pending", "omo_pending_watch.py"),
            ("email", "email_idle_watcher.py"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                old_root = tmp / "old-work-logs"
                state = tmp / "state"
                old_root.mkdir()
                state.mkdir()
                token = f"old-{name}-token"
                launch_pid_file = state / f".{name}-supervisor.{token}.pid"
                child_pid_file = tmp / f"old-{name}-child.pid"
                args = [
                    "bash",
                    "-c",
                    f'sleep 30 & printf \'%s\\n\' "$!" >"{child_pid_file}"; wait # {name} watcher exited status',
                    f"{name}-watch-supervisor",
                    str(launch_pid_file),
                    token,
                    "uv",
                    "run",
                    "--project",
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / script_name),
                    "--root",
                    str(old_root),
                ]
                if name == "email":
                    args.extend(("--mail-dir", str(old_root / "manager_mail"), "--state-dir", str(state)))
                current = subprocess.Popen(args, start_new_session=True)
                child_pid: int | None = None
                try:
                    self.wait_for_file(child_pid_file)
                    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                    pidfile = state / f"{name}-supervisor.pid"
                    pidfile_contents = self.supervisor_pidfile_contents(current.pid, token, old_root)
                    pidfile.write_text(
                        pidfile_contents,
                        encoding="utf-8",
                    )
                    result = self.run_setup(tmp)
                    self.assertEqual(1, result.returncode, result.stderr)
                    self.assertIsNone(current.poll())
                    self.assertTrue(self.process_active(child_pid))
                    self.assertIn(
                        f"authenticated {name} watcher pidfile has no matching launch root identity; refusing replacement",
                        result.stderr,
                    )
                    self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                    self.assertFalse((tmp / "fake-uv.log").exists())
                finally:
                    if current.poll() is None:
                        self.terminate_tree(current.pid)
                        current.wait(timeout=2)
                    if child_pid is not None and self.process_active(child_pid):
                        os.kill(child_pid, signal.SIGKILL)
                    self.stop_supervisors(state)

    def test_setup_preserves_same_path_supervisor_after_cross_run_root_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            root.mkdir()
            state.mkdir()
            current = self.start_owned_pending_supervisor(root, state, "cross-run-root-replacement")
            pidfile = state / "pending-supervisor.pid"
            pidfile_contents = pidfile.read_text(encoding="utf-8")
            root.rename(tmp / "work_logs.moved")
            root.mkdir()
            try:
                result = self.run_setup(tmp)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn(
                    "authenticated pending watcher pidfile has no matching launch root identity; refusing replacement",
                    result.stderr,
                )
                self.assertIsNone(current.poll())
                self.assertEqual(pidfile_contents, pidfile.read_text(encoding="utf-8"))
                self.assertFalse((tmp / "fake-uv.log").exists())
            finally:
                if current.poll() is None:
                    self.terminate_tree(current.pid)
                    current.wait(timeout=2)

    def test_setup_replaces_authenticated_supervisors_with_transitional_pidfiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            try:
                first = self.run_setup(tmp)
                self.assertEqual(0, first.returncode, first.stdout + first.stderr)
                root_stat = root.stat()
                old_pids: dict[Path, int] = {}
                pending_pidfile = state / "pending-supervisor.pid"
                pending_text = pending_pidfile.read_text(encoding="utf-8")
                pending_pid = self.pid_from_file(pending_pidfile)
                self.assertIsNotNone(pending_pid)
                assert pending_pid is not None
                old_pids[pending_pidfile] = pending_pid
                pending_pidfile.write_text(
                    "\n".join(
                        line
                        for line in pending_text.splitlines()
                        if not line.startswith(("root_dev=", "root_ino="))
                    )
                    + "\n",
                    encoding="utf-8",
                )
                email_token = "a" * 32
                email_launch = state / f".email-supervisor.{email_token}.pid"
                email = subprocess.Popen(
                    [
                        "bash",
                        "-c",
                        "while :; do sleep 30; done # email watcher exited status",
                        "email-watch-supervisor",
                        str(email_launch),
                        email_token,
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
                email_pidfile = state / "email-supervisor.pid"
                email_pidfile.write_text(
                    f"pid={email.pid}\nstart={self.process_start_ticks(email.pid)}\ntoken={email_token}\n",
                    encoding="utf-8",
                )

                second = self.run_setup(tmp)

                self.assertEqual(0, second.returncode, second.stdout + second.stderr)
                email.wait(timeout=2)
                self.assertFalse(email_pidfile.exists())
                for pidfile, old_pid in old_pids.items():
                    self.wait_for_process_exit(old_pid)
                    if pidfile.exists():
                        refreshed = pidfile.read_text(encoding="utf-8")
                        self.assertIn(f"root_dev={root_stat.st_dev}\n", refreshed)
                        self.assertIn(f"root_ino={root_stat.st_ino}\n", refreshed)
                        self.assertNotEqual(old_pid, self.pid_from_file(pidfile))
            finally:
                self.stop_supervisors(state)

    def test_setup_refuses_present_invalid_or_duplicate_root_identity(self) -> None:
        for identity_lines in (
            "root_dev=\nroot_ino=\n",
            "root_dev={dev}\nroot_dev=1\nroot_ino={ino}\n",
            "root_dev\nroot_ino = {ino}\n",
        ):
            with self.subTest(identity_lines=identity_lines), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp)
                root = tmp / "work_logs"
                state = tmp / "state"
                root.mkdir()
                state.mkdir()
                current = self.start_owned_pending_supervisor(root, state, "invalid-root-identity")
                pidfile = state / "pending-supervisor.pid"
                root_stat = root.stat()
                identity_lines = identity_lines.format(dev=root_stat.st_dev, ino=root_stat.st_ino)
                base = "\n".join(
                    line
                    for line in pidfile.read_text(encoding="utf-8").splitlines()
                    if not line.startswith(("root_dev=", "root_ino="))
                )
                pidfile.write_text(f"{base}\n{identity_lines}", encoding="utf-8")
                try:
                    result = self.run_setup(tmp)
                    self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                    self.assertIsNone(current.poll())
                    self.assertIn(
                        "authenticated pending watcher pidfile has no matching launch root identity; refusing replacement",
                        result.stderr,
                    )
                finally:
                    if current.poll() is None:
                        self.terminate_tree(current.pid)
                        current.wait(timeout=2)

    def test_setup_does_not_kill_pidfile_supervisor_from_another_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            old_root = tmp / "old-work-logs"
            state = tmp / "state"
            other_state = tmp / "other-state"
            old_root.mkdir()
            state.mkdir()
            other_state.mkdir()
            token = "other-state-token"
            current = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    "while :; do sleep 30; done # pending watcher exited status",
                    "pending-watch-supervisor",
                    str(other_state / f".pending-supervisor.{token}.pid"),
                    token,
                    "uv",
                    "run",
                    "--project",
                    str(ROOT / "omo_manager"),
                    str(ROOT / "omo_manager" / "omo_pending_watch.py"),
                    "--root",
                    str(old_root),
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
                self.assertIsNone(current.poll())
                self.assertIn("stale pending watcher pidfile points at unowned", result.stderr)
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

    def test_setup_replaces_legacy_email_supervisor_for_same_root_and_mail_dir_with_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            old_state = tmp / "old-state"
            root.mkdir()
            state.mkdir()
            old_state.mkdir()
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
                    str(old_state),
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

    def test_setup_replaces_current_format_email_supervisor_for_same_root_and_mail_dir_with_stale_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            old_state = tmp / "old-state"
            root.mkdir()
            state.mkdir()
            old_state.mkdir()
            old_launch_pid_file = old_state / ".email-supervisor.oldtoken.pid"
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
                    str(old_state),
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

    def test_setup_preserves_email_supervisor_for_same_root_but_different_mail_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            other_mail = tmp / "other-mail"
            root.mkdir()
            state.mkdir()
            other_mail.mkdir()
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
                    str(other_mail),
                    "--state-dir",
                    str(tmp / "old-state"),
                ]
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn(f"stopping legacy email watcher supervisor pid={legacy.pid}", result.stdout)
                self.assertIsNone(legacy.poll())
            finally:
                if legacy.poll() is None:
                    try:
                        os.killpg(legacy.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    legacy.wait(timeout=2)
                self.stop_supervisors(state)

    def test_setup_preserves_current_format_email_supervisor_for_same_root_but_different_mail_dir(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            root = tmp / "work_logs"
            state = tmp / "state"
            old_state = tmp / "old-state"
            other_mail = tmp / "other-mail"
            root.mkdir()
            state.mkdir()
            old_state.mkdir()
            other_mail.mkdir()
            old_launch_pid_file = old_state / ".email-supervisor.oldtoken.pid"
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
                    str(other_mail),
                    "--state-dir",
                    str(old_state),
                ],
                start_new_session=True,
            )
            try:
                result = self.run_setup(tmp)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn(f"stopping legacy email watcher supervisor pid={current.pid}", result.stdout)
                self.assertIsNone(current.poll())
            finally:
                if current.poll() is None:
                    try:
                        os.killpg(current.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    current.wait(timeout=2)
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

    def test_root_change_during_email_health_failure_preserves_launched_supervisors(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            state = tmp / "state"
            try:
                result = self.run_setup(tmp, fake_uv_mode="email-health-root-swap", email="true")
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn("work-log root changed before watcher teardown", result.stderr)
                self.assertTrue((tmp / "work_logs.moved").is_dir())
                for name in ("pending", "email"):
                    pidfile = state / f"{name}-supervisor.pid"
                    self.assertTrue(pidfile.exists())
                    pid = self.pid_from_file(pidfile)
                    self.assertIsNotNone(pid)
                    assert pid is not None
                    self.assertTrue(self.process_active(pid))
            finally:
                self.stop_supervisors(state)

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
