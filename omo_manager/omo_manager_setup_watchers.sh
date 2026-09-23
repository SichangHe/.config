#!/usr/bin/env bash
set -euo pipefail
PATH="$HOME/.config/bin:$PATH"
helper_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
helper_dir="$(cd "$(dirname "$helper_path")" && pwd)"
uv_run=()
if command -v uv >/dev/null 2>&1; then
  uv_run=(uv run --project "$helper_dir")
fi
case "${1:-}" in
  -h|--help)
    cat <<'EOF'
Usage: omo_manager_setup_watchers.sh
Start or refresh manager pending and email watchers.
EOF
    exit 0
    ;;
esac
if [ -n "${TMUX:-}" ] \
  && [ "${OMO_MANAGER_TMUX_REENTRY:-0}" != 1 ] \
  && command -v tmux >/dev/null 2>&1 \
  && tmux display-message -p '#{pid}' >/dev/null 2>&1; then
  umask 077
  bridge_dir="$(mktemp -d "${TMPDIR:-/tmp}/omo-manager-watchers.XXXXXX")"
  bridge_cleanup=1
  trap '[ "$bridge_cleanup" -eq 0 ] || rm -rf -- "$bridge_dir"' EXIT
  bridge_env="$bridge_dir/environment"
  bridge_stdout="$bridge_dir/stdout"
  bridge_stderr="$bridge_dir/stderr"
  bridge_status="$bridge_dir/status"
  bridge_state="$bridge_dir/state"
  bridge_timeout_raw="${OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S:-60}"
  case "$bridge_timeout_raw" in
    ''|*[!0-9]*)
      echo "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S must be a positive integer" >&2
      exit 2
      ;;
  esac
  bridge_timeout_s=$((10#$bridge_timeout_raw))
  if [ "$bridge_timeout_s" -eq 0 ]; then
    echo "OMO_MANAGER_TMUX_HANDOFF_TIMEOUT_S must be a positive integer" >&2
    exit 2
  fi
  export -p >"$bridge_env"
  printf 'export OMO_MANAGER_TMUX_REENTRY=1\n' >>"$bridge_env"
  printf 'queued\n' >"$bridge_state"

  printf -v bridge_invocation '%q ' "$helper_path" "$@"
  printf -v bridge_payload \
    'set +e; if source %q; then rm -f %q; printf "running\\n" >%q.tmp; mv -f %q.tmp %q; %s>%q 2>%q; status=$?; final_state=complete; else status=125; final_state=failed; printf "tmux watcher setup handoff environment unavailable\\n" >%q; fi; printf "%%s\\n" "$final_state" >%q.tmp; mv -f %q.tmp %q; printf "%%s\\n" "$status" >%q.tmp; mv -f %q.tmp %q; exit "$status"' \
    "$bridge_env" "$bridge_env" "$bridge_state" "$bridge_state" "$bridge_state" \
    "$bridge_invocation" "$bridge_stdout" "$bridge_stderr" "$bridge_stderr" \
    "$bridge_state" "$bridge_state" "$bridge_state" \
    "$bridge_status" "$bridge_status" "$bridge_status"
  printf -v bridge_command 'exec bash -c %q' "$bridge_payload"
  if ! tmux run-shell -b "$bridge_command"; then
    echo "failed to queue watcher setup through tmux" >&2
    exit 1
  fi
  bridge_deadline=$((SECONDS + bridge_timeout_s))
  while [ ! -s "$bridge_status" ] && [ "$SECONDS" -lt "$bridge_deadline" ]; do
    sleep 0.1
  done
  cat "$bridge_stdout" 2>/dev/null || true
  cat "$bridge_stderr" >&2 2>/dev/null || true
  if [ ! -s "$bridge_status" ]; then
    rm -f "$bridge_env"
    bridge_cleanup=0
    bridge_current_state="$(sed -n '1p' "$bridge_state" 2>/dev/null || true)"
    echo "timed out waiting for tmux watcher setup after ${bridge_timeout_s}s; handoff state=${bridge_current_state:-unknown} path=$bridge_dir" >&2
    exit 1
  fi
  bridge_result="$(sed -n '1p' "$bridge_status" 2>/dev/null || true)"
  case "$bridge_result" in
    ''|*[!0-9]*)
      echo "tmux watcher setup did not report its status" >&2
      exit 1
      ;;
  esac
  exit "$bridge_result"
fi
env_manager_url="${OMO_MANAGER_URL+x}${OMO_MANAGER_URL-}"
env_root="${OMO_WORK_LOGS_ROOT+x}${OMO_WORK_LOGS_ROOT-}"
env_state_dir="${OMO_MANAGER_STATE_DIR+x}${OMO_MANAGER_STATE_DIR-}"
env_mail_dir="${OMO_MANAGER_MAIL_DIR+x}${OMO_MANAGER_MAIL_DIR-}"
env_email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER+x}${OMO_MANAGER_ENABLE_EMAIL_WATCHER-}"
env_audit_enable="${OMO_MANAGER_ENABLE_AGENT_AUDIT+x}${OMO_MANAGER_ENABLE_AGENT_AUDIT-}"
env_email_config="${OMO_EMAIL_CONFIG_PATH+x}${OMO_EMAIL_CONFIG_PATH-}"
env_agent_email="${OMO_AGENT_GMAIL_ADDRESS+x}${OMO_AGENT_GMAIL_ADDRESS-}"
env_agent_password="${OMO_AGENT_GMAIL_APP_PASSWORD+x}${OMO_AGENT_GMAIL_APP_PASSWORD-}"
env_human_email="${OMO_HUMAN_EMAIL_ADDRESS+x}${OMO_HUMAN_EMAIL_ADDRESS-}"
env_human_config="${OMO_HUMAN_EMAIL_CONFIG_PATH+x}${OMO_HUMAN_EMAIL_CONFIG_PATH-}"
env_default_contact_agent="${DEFAULT_CONTACT_AGENT+x}${DEFAULT_CONTACT_AGENT-}"
env_guest_hees_email_enable="${OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER+x}${OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER-}"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
configured_root="${OMO_WORK_LOGS_ROOT:-}"
inherited_root="${env_root#x}"
# 🧑 "Normalize only trailing slashes for lexical final-component validation, preserving `/` ... and derive identity from that same lexical form."
lexical_root_path() {
  local lexical_candidate="$1"
  while :; do
    while [ "$lexical_candidate" != / ] && [[ "$lexical_candidate" == */ ]]; do
      lexical_candidate="${lexical_candidate%/}"
    done
    if [[ "$lexical_candidate" == */. ]]; then
      lexical_candidate="${lexical_candidate%/.}"
      [ -n "$lexical_candidate" ] || lexical_candidate=/
      continue
    fi
    break
  done
  printf '%s\n' "$lexical_candidate"
}
require_real_root() {
  local candidate="$1" lexical_candidate
  lexical_candidate="$(lexical_root_path "$candidate")"
  if [ -L "$lexical_candidate" ]; then
    echo "work-log root must be a real directory, not a symlink: $candidate" >&2
    exit 2
  fi
  if [ ! -d "$lexical_candidate" ]; then
    echo "work-log root must be an existing directory: $candidate" >&2
    exit 2
  fi
}
if [ -n "$inherited_root" ]; then
  require_real_root "$inherited_root"
fi
if [ -n "$configured_root" ]; then
  require_real_root "$configured_root"
fi
if [ -n "$inherited_root" ] && [ -n "$configured_root" ] && [ "$inherited_root" != "$configured_root" ]; then
  inherited_root_resolved="$(readlink -f -- "$inherited_root" 2>/dev/null || true)"
  configured_root_resolved="$(readlink -f -- "$configured_root" 2>/dev/null || true)"
  if [ -z "$inherited_root_resolved" ] || [ "$inherited_root_resolved" != "$configured_root_resolved" ]; then
    echo "OMO_WORK_LOGS_ROOT conflicts with $local_env; refusing to replace configured watchers" >&2
    exit 2
  fi
fi
[ -n "${env_manager_url#x}" ] && OMO_MANAGER_URL="${env_manager_url#x}"
[ -n "${env_root#x}" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
[ -n "${env_state_dir#x}" ] && OMO_MANAGER_STATE_DIR="${env_state_dir#x}"
[ -n "${env_mail_dir#x}" ] && OMO_MANAGER_MAIL_DIR="${env_mail_dir#x}"
[ -n "${env_email_enable#x}" ] && OMO_MANAGER_ENABLE_EMAIL_WATCHER="${env_email_enable#x}"
[ -n "${env_audit_enable#x}" ] && OMO_MANAGER_ENABLE_AGENT_AUDIT="${env_audit_enable#x}"
[ -n "${env_email_config#x}" ] && OMO_EMAIL_CONFIG_PATH="${env_email_config#x}"
[ -n "${env_agent_email#x}" ] && OMO_AGENT_GMAIL_ADDRESS="${env_agent_email#x}"
[ -n "${env_agent_password#x}" ] && OMO_AGENT_GMAIL_APP_PASSWORD="${env_agent_password#x}"
[ -n "${env_human_email#x}" ] && OMO_HUMAN_EMAIL_ADDRESS="${env_human_email#x}"
[ -n "${env_human_config#x}" ] && OMO_HUMAN_EMAIL_CONFIG_PATH="${env_human_config#x}"
[ -n "${env_default_contact_agent#x}" ] && DEFAULT_CONTACT_AGENT="${env_default_contact_agent#x}"
[ -n "${env_guest_hees_email_enable#x}" ] && OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER="${env_guest_hees_email_enable#x}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
root_lexical="$(lexical_root_path "$root")"
require_real_root "$root"
root_identity="$(stat -c '%d:%i' -- "$root_lexical" 2>/dev/null || true)"
if [ -z "$root_identity" ]; then
  echo "work-log root identity is unavailable: $root" >&2
  exit 2
fi
require_root_identity() {
  local current_identity
  require_real_root "$root"
  current_identity="$(stat -c '%d:%i' -- "$root_lexical" 2>/dev/null || true)"
  if [ -z "$current_identity" ] || [ "$current_identity" != "$root_identity" ]; then
    echo "work-log root changed before watcher teardown: $root" >&2
    exit 2
  fi
}
require_root_identity
manager_url="${OMO_MANAGER_URL:-}"
manager_target="${OMO_MANAGER_TMUX_TARGET:-}"
default_contact_agent="${DEFAULT_CONTACT_AGENT:-}"
guest_hees_email_enable="${OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER:-false}"
state_base="${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager"
state_dir="${OMO_MANAGER_STATE_DIR:-$state_base}"
email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER:-auto}"
audit_enable="${OMO_MANAGER_ENABLE_AGENT_AUDIT:-false}"
email_config="${OMO_EMAIL_CONFIG_PATH:-$HOME/.config/himalaya/config.toml}"
agent_email="${OMO_AGENT_GMAIL_ADDRESS:-}"
agent_password="${OMO_AGENT_GMAIL_APP_PASSWORD:-}"
human_email="${OMO_HUMAN_EMAIL_ADDRESS:-}"
mail_dir="${OMO_MANAGER_MAIL_DIR:-$root/manager_mail}"
email_supervisor_startup_grace_s="${OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S:-2}"
watcher_health_timeout_s="${OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S:-5}"
case "$email_supervisor_startup_grace_s" in
  ''|*[!0-9]*) echo "OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S must be a non-negative integer" >&2; exit 2 ;;
esac
case "$watcher_health_timeout_s" in
  ''|*[!0-9]*) echo "OMO_MANAGER_WATCHER_HEALTH_TIMEOUT_S must be a non-negative integer" >&2; exit 2 ;;
esac
case "$audit_enable" in
  1|true|yes|on) start_audit=1 ;;
  0|false|no|off) start_audit=0 ;;
  *) echo "OMO_MANAGER_ENABLE_AGENT_AUDIT must be true or false" >&2; exit 2 ;;
esac
case "$guest_hees_email_enable" in
  1|true|yes|on) start_guest_hees_email=1 ;;
  0|false|no|off) start_guest_hees_email=0 ;;
  *) echo "OMO_MANAGER_ENABLE_GUEST_HEES_EMAIL_WATCHER must be true or false" >&2; exit 2 ;;
esac
export OMO_MANAGER_URL="$manager_url"
export OMO_MANAGER_TMUX_TARGET="$manager_target"
export DEFAULT_CONTACT_AGENT="$default_contact_agent"
export OMO_WORK_LOGS_ROOT="$root"
export OMO_MANAGER_STATE_DIR="$state_dir"
export OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S="$email_supervisor_startup_grace_s"
export OMO_MANAGER_MAIL_DIR="$mail_dir"
export OMO_AGENT_GMAIL_ADDRESS="$agent_email"
export OMO_AGENT_GMAIL_APP_PASSWORD="$agent_password"
export OMO_HUMAN_EMAIL_ADDRESS="$human_email"
export OMO_HUMAN_EMAIL_CONFIG_PATH="${OMO_HUMAN_EMAIL_CONFIG_PATH:-$email_config}"
mkdir -p -m 700 "$state_dir"
chmod 700 "$state_dir"
exec 8>"$state_dir/watchers.lock"
if ! flock -n 8; then
  echo "watcher setup already running for $state_dir" >&2
  exit 1
fi
if [ -z "$manager_url" ] && [ -z "$manager_target" ]; then
  echo "OMO_MANAGER_TMUX_TARGET or OMO_MANAGER_URL is required" >&2
  exit 2
fi
echo "manager_target=${manager_target:-unset} manager_url=${manager_url:-unset}"
start_email=0
split_email_values=0
[ -n "$agent_email" ] && split_email_values=$((split_email_values + 1))
[ -n "$agent_password" ] && split_email_values=$((split_email_values + 1))
[ -n "$human_email" ] && split_email_values=$((split_email_values + 1))
if [ "$split_email_values" -ne 0 ] && [ "$split_email_values" -ne 3 ]; then
  echo "split email setup requires OMO_AGENT_GMAIL_ADDRESS, OMO_AGENT_GMAIL_APP_PASSWORD, and OMO_HUMAN_EMAIL_ADDRESS together" >&2
  exit 2
fi
if [ "$start_guest_hees_email" -eq 1 ] && [ "$split_email_values" -ne 3 ]; then
  echo "guest-hees email watcher requires split email setup" >&2
  exit 2
fi
case "$email_enable" in
  1|true|yes) start_email=1 ;;
  0|false|no) start_email=0 ;;
  auto) { [ "$split_email_values" -eq 3 ] || [ -f "$email_config" ]; } && start_email=1 || start_email=0 ;;
  *) echo "OMO_MANAGER_ENABLE_EMAIL_WATCHER must be auto, true, or false" >&2; exit 2 ;;
esac

pid_file() {
  printf '%s/%s-supervisor.pid\n' "$state_dir" "$1"
}

valid_pid() {
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

process_alive() {
  local pid="${1:-}"
  valid_pid "$pid" && kill -0 "$pid" 2>/dev/null
}

process_active() {
  local pid="${1:-}" stat rest
  process_alive "$pid" || return 1
  [ -r "/proc/$pid/stat" ] || return 1
  stat="$(<"/proc/$pid/stat")"
  rest="${stat##*) }"
  set -- $rest
  [ "${1:-}" != Z ]
}

# 🧑 "... without weakening authenticated readiness"
ready_process_is_active() {
  local pid="${1:-}" stat rest
  process_alive "$pid" || return 1
  [ -r "/proc/$pid/stat" ] || return 1
  stat="$(<"/proc/$pid/stat")"
  rest="${stat##*) }"
  set -- $rest
  case "${1:-}" in
    ''|Z|T|t|X|x) return 1 ;;
  esac
}

owner_token() {
  od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
}

cmdline_has_arg() {
  local pid="$1" value="$2" arg
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    [ "$arg" = "$value" ] && return 0
  done <"/proc/$pid/cmdline"
  return 1
}

same_resolved_path() {
  local left="$1" right="$2" resolved_left resolved_right
  resolved_left="$(readlink -f -- "$left" 2>/dev/null || true)"
  resolved_right="$(readlink -f -- "$right" 2>/dev/null || true)"
  [ -n "$resolved_left" ] && [ "$resolved_left" = "$resolved_right" ]
}

cmdline_has_resolved_path_arg() {
  local pid="$1" value="$2" arg
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    same_resolved_path "$arg" "$value" && return 0
  done <"/proc/$pid/cmdline"
  return 1
}

cmdline_has_arg_pair() {
  local pid="$1" option="$2" value="$3" arg expect=0
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    if [ "$expect" -eq 1 ]; then
      [ "$arg" = "$value" ] && return 0
      expect=0
    fi
    [ "$arg" = "$option" ] && expect=1
  done <"/proc/$pid/cmdline"
  return 1
}

cmdline_has_option_value() {
  local pid="$1" option="$2" arg expect=0
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    if [ "$expect" -eq 1 ]; then
      [ -n "$arg" ] && return 0
      expect=0
    fi
    [ "$arg" = "$option" ] && expect=1
  done <"/proc/$pid/cmdline"
  return 1
}

cmdline_has_resolved_path_arg_pair() {
  local pid="$1" option="$2" value="$3" arg expect=0
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    if [ "$expect" -eq 1 ]; then
      same_resolved_path "$arg" "$value" && return 0
      expect=0
    fi
    [ "$arg" = "$option" ] && expect=1
  done <"/proc/$pid/cmdline"
  return 1
}

cmdline_has_fragment() {
  local pid="$1" fragment="$2" arg
  [ -r "/proc/$pid/cmdline" ] || return 1
  while IFS= read -r -d '' arg; do
    [[ "$arg" == *"$fragment"* ]] && return 0
  done <"/proc/$pid/cmdline"
  return 1
}

process_start_ticks() {
  local pid="$1" stat rest
  [ -r "/proc/$pid/stat" ] || return 1
  stat="$(<"/proc/$pid/stat")"
  rest="${stat##*) }"
  set -- $rest
  [ -n "${20:-}" ] || return 1
  printf '%s\n' "${20}"
}

process_session_id() {
  local pid="$1" stat rest
  [ -r "/proc/$pid/stat" ] || return 1
  stat="$(<"/proc/$pid/stat")"
  rest="${stat##*) }"
  set -- $rest
  [ -n "${4:-}" ] || return 1
  printf '%s\n' "${4}"
}

watcher_runtime_process() {
  local pid="$1" script_path="$2" argv0 argv1 exe base0
  local -a argv=()
  [ -r "/proc/$pid/cmdline" ] || return 1
  mapfile -d '' -t argv <"/proc/$pid/cmdline" || return 1
  exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
  argv0="${argv[0]:-}"
  argv1="${argv[1]:-}"
  base0="${argv0##*/}"
  [[ "${exe##*/}" == python* && "$base0" == python* && "$argv1" = "$script_path" ]]
}

process_parent_id() {
  local pid="$1" stat rest
  [ -r "/proc/$pid/stat" ] || return 1
  stat="$(<"/proc/$pid/stat")"
  rest="${stat##*) }"
  set -- $rest
  [ -n "${2:-}" ] || return 1
  printf '%s\n' "${2}"
}

process_has_ancestor() {
  local pid="$1" ancestor="$2" parent
  while parent="$(process_parent_id "$pid" 2>/dev/null)"; do
    [ "$parent" = "$ancestor" ] && return 0
    if [ "$parent" = 0 ] || [ "$parent" = 1 ] || [ "$parent" = "$pid" ]; then
      return 1
    fi
    pid="$parent"
  done
  return 1
}

supervisor_guardian_code="$(cat <<'PY'
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

SUBREAPER_MARKER = "omo-watcher-subreaper-v1"
PR_SET_CHILD_SUBREAPER = 36

if len(sys.argv) < 4 or sys.argv[1] != SUBREAPER_MARKER:
    raise SystemExit(125)
libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
    error = ctypes.get_errno()
    print(f"watcher supervisor could not enable child subreaping: errno={error}", file=sys.stderr)
    raise SystemExit(125)

launch_pid_file = Path(sys.argv[2])
command = sys.argv[3:]
os.umask(0o077)
temporary_pid_file = launch_pid_file.with_name(f"{launch_pid_file.name}.tmp.{os.getpid()}")
temporary_pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
os.chmod(temporary_pid_file, 0o600)
os.replace(temporary_pid_file, launch_pid_file)

stop_signal = 0


def request_stop(signum: int, _frame: object) -> None:
    global stop_signal
    if stop_signal == 0:
        stop_signal = signum


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)
child = subprocess.Popen(command)
main_status: int | None = None
termination_started: float | None = None


def reap_children() -> None:
    global main_status
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return
        if pid == child.pid:
            main_status = os.waitstatus_to_exitcode(status)


def direct_children() -> list[int]:
    try:
        contents = Path(f"/proc/self/task/{os.getpid()}/children").read_text(encoding="ascii")
    except OSError:
        return []
    return [int(value) for value in contents.split()]


while True:
    reap_children()
    if main_status is not None and stop_signal == 0:
        stop_signal = signal.SIGTERM
    if stop_signal != 0:
        if termination_started is None:
            termination_started = time.monotonic()
            try:
                os.killpg(os.getpgrp(), signal.SIGTERM)
            except ProcessLookupError:
                pass
        children = direct_children()
        if not children:
            raise SystemExit(main_status if main_status is not None else 128 + stop_signal)
        cleanup_signal = signal.SIGKILL if time.monotonic() - termination_started >= 1.0 else signal.SIGTERM
        for pid in children:
            try:
                os.kill(pid, cleanup_signal)
            except ProcessLookupError:
                pass
        time.sleep(0.02)
        continue
    time.sleep(0.05)
PY
)"

supervisor_has_watcher_descendant() {
  local supervisor_pid="$1" script_path="$2" pid
  while read -r pid; do
    [ -n "$pid" ] || continue
    if watcher_runtime_process "$pid" "$script_path" && process_has_ancestor "$pid" "$supervisor_pid"; then
      return 0
    fi
  done < <(descendant_pids "$supervisor_pid")
  return 1
}

descendant_pids() {
  local parent="$1" child
  pgrep -P "$parent" 2>/dev/null | while read -r child; do
    [ -n "$child" ] || continue
    printf '%s\n' "$child"
    descendant_pids "$child"
  done
}

record_active_targets() {
  local pid start
  for pid in "$@"; do
    start="$(process_start_ticks "$pid" 2>/dev/null || true)"
    [ -n "$start" ] && process_active "$pid" && printf '%s:%s\n' "$pid" "$start"
  done
}

known_active_pids() {
  local record pid start current_start
  for record in "$@"; do
    pid="${record%%:*}"
    start="${record#*:}"
    current_start="$(process_start_ticks "$pid" 2>/dev/null || true)"
    [ -n "$current_start" ] && [ "$current_start" = "$start" ] && process_active "$pid" && printf '%s\n' "$pid"
  done
}

# 🧑 "... prove every supervisor-tree PID is gone before setup returns."
stop_process_tree() {
  local pid="$1" stop_signal attempt
  local -a descendants=() discovered=() records=() extra_records=() live=() survivor_records=()
  mapfile -t descendants < <(descendant_pids "$pid")
  mapfile -t records < <(record_active_targets "$pid" "${descendants[@]}")
  for stop_signal in TERM KILL; do
    for attempt in 1 2 3 4 5 6; do
      mapfile -t discovered < <({
        pgrep -g "$pid" 2>/dev/null || true
        pgrep -s "$pid" 2>/dev/null || true
        for target in "${live[@]}"; do
          descendant_pids "$target"
        done
      } | awk 'NF && !seen[$0]++')
      mapfile -t extra_records < <(record_active_targets "${discovered[@]}")
      mapfile -t records < <(printf '%s\n' "${records[@]}" "${extra_records[@]}" | awk 'NF && !seen[$0]++')
      mapfile -t live < <(known_active_pids "${records[@]}" | awk 'NF && !seen[$0]++')
      if [ "${#live[@]}" -eq 0 ]; then
        mapfile -t discovered < <({ pgrep -g "$pid" 2>/dev/null || true; pgrep -s "$pid" 2>/dev/null || true; } | awk 'NF && !seen[$0]++')
        mapfile -t extra_records < <(record_active_targets "${discovered[@]}")
        [ "${#extra_records[@]}" -eq 0 ] && return 0
        mapfile -t records < <(printf '%s\n' "${records[@]}" "${extra_records[@]}" | awk 'NF && !seen[$0]++')
        continue
      fi
      [ "$attempt" -eq 6 ] && break
      kill -s "$stop_signal" -- "-$pid" >/dev/null 2>&1 || true
      kill -s "$stop_signal" "${live[@]}" >/dev/null 2>&1 || true
      sleep 0.2
    done
  done
  mapfile -t survivor_records < <(record_active_targets "${live[@]}")
  [ "${#survivor_records[@]}" -eq 0 ] && return 0
  printf 'failed to stop process tree rooted at pid %s; live pid:start=%s\n' "$pid" "${survivor_records[*]}" >&2
  return 1
}

stop_subreaper_supervisor() {
  local pid="$1" start="$2" current_start
  kill -TERM "$pid" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do
    current_start="$(process_start_ticks "$pid" 2>/dev/null || true)"
    if [ -z "$current_start" ] || [ "$current_start" != "$start" ] || ! process_active "$pid"; then
      return 0
    fi
    sleep 0.05
  done
  echo "failed to stop subreaper-contained supervisor pid=$pid start=$start; retaining recovery pidfile" >&2
  return 1
}

write_pidfile() {
  local name="$1" pid="$2" token="$3" containment="${4:-}" file start root_dev root_ino
  file="$(pid_file "$name")"
  start="$(process_start_ticks "$pid")"
  root_dev="${root_identity%%:*}"
  root_ino="${root_identity#*:}"
  {
    printf 'pid=%s\n' "$pid"
    printf 'start=%s\n' "$start"
    printf 'token=%s\n' "$token"
    printf 'root_dev=%s\n' "$root_dev"
    printf 'root_ino=%s\n' "$root_ino"
    [ -z "$containment" ] || printf 'containment=%s\n' "$containment"
  } >"$file"
  chmod 600 "$file"
}

pidfile_value() {
  local file="$1" key="$2"
  sed -n "s/^$key=//p" "$file" 2>/dev/null | sed -n '1p'
}

owned_supervisor_process() {
  local pid="$1" start="$2" token="$3" name="$4" script_path="$5" root_arg="$6" state_arg="${7:-}" loop_marker="${8:-}" root_match_mode="${9:-0}" containment="${10:-}" current_start session_id
  process_alive "$pid" || return 1
  current_start="$(process_start_ticks "$pid")" || return 1
  [ "$current_start" = "$start" ] || return 1
  session_id="$(process_session_id "$pid")" || return 1
  [ "$session_id" = "$pid" ] || return 1
  cmdline_has_fragment "$pid" "$loop_marker" || return 1
  cmdline_has_arg "$pid" "$name-watch-supervisor" || return 1
  cmdline_has_resolved_path_arg "$pid" "$state_dir/.$name-supervisor.$token.pid" || return 1
  cmdline_has_arg "$pid" "$token" || return 1
  cmdline_has_resolved_path_arg "$pid" "$script_path" || return 1
  case "$root_match_mode" in
    0) cmdline_has_resolved_path_arg_pair "$pid" --root "$root_arg" || return 1 ;;
    1) cmdline_has_arg_pair "$pid" --root "$root_arg" || return 1 ;;
    2) cmdline_has_option_value "$pid" --root || return 1 ;;
    *) return 1 ;;
  esac
  case "$containment" in
    '') ;;
    subreaper-v1) cmdline_has_arg "$pid" omo-watcher-subreaper-v1 || return 1 ;;
    *) return 1 ;;
  esac
  [ -z "$state_arg" ] || cmdline_has_arg_pair "$pid" --state-dir "$state_arg"
}

stop_owned_supervisor() {
  local name="$1" pid="$2" start="$3" token="$4" script_path="$5" root_arg="$6" state_arg="${7:-}" loop_marker="${8:-}" root_match_mode="${9:-0}" containment="${10:-}"
  if owned_supervisor_process "$pid" "$start" "$token" "$name" "$script_path" "$root_arg" "$state_arg" "$loop_marker" "$root_match_mode" "$containment"; then
    if [ "$root_match_mode" -eq 0 ]; then
      require_root_identity
    fi
    if [ "$containment" = subreaper-v1 ]; then
      stop_subreaper_supervisor "$pid" "$start"
    else
      stop_process_tree "$pid"
    fi
    return
  fi
  return 1
}

rollback_launched_supervisor() {
  local name="$1" pid="$2" start="$3" token="$4" script_path="$5" root_arg="$6" state_arg="${7:-}" loop_marker="${8:-}" containment="${9:-}" stopped=0
  require_root_identity
  if stop_owned_supervisor "$name" "$pid" "$start" "$token" "$script_path" "$root_arg" "$state_arg" "$loop_marker" 0 "$containment"; then
    stopped=1
  elif [ "$containment" != subreaper-v1 ] && ! process_active "$pid" && stop_process_tree "$pid"; then
    stopped=1
  fi
  if [ "$stopped" -ne 1 ]; then
    echo "authenticated $name watcher supervisor tree is still live; retaining pidfile" >&2
    return 1
  fi
  require_root_identity
  rm -f "$(pid_file "$name")"
}

stop_pidfile_supervisor() {
  local name="$1" script_path="$2" root_arg="$3" state_arg="${4:-}" loop_marker="${5:-}" file pid start token containment pid_root_dev pid_root_ino pid_root_dev_count pid_root_ino_count
  file="$(pid_file "$name")"
  if [ ! -r "$file" ]; then
    return 0
  fi
  pid="$(pidfile_value "$file" pid)"
  start="$(pidfile_value "$file" start)"
  token="$(pidfile_value "$file" token)"
  pid_root_dev="$(pidfile_value "$file" root_dev)"
  pid_root_ino="$(pidfile_value "$file" root_ino)"
  pid_root_dev_count="$(grep -c '^root_dev' "$file" 2>/dev/null || true)"
  pid_root_ino_count="$(grep -c '^root_ino' "$file" 2>/dev/null || true)"
  containment="$(pidfile_value "$file" containment)"
  if process_alive "$pid"; then
    if owned_supervisor_process "$pid" "$start" "$token" "$name" "$script_path" "$root_arg" "$state_arg" "$loop_marker" 2 "$containment"; then
      if [[ ! "$pid_root_dev" =~ ^[0-9]+$ ]] \
        || [[ ! "$pid_root_ino" =~ ^[0-9]+$ ]] \
        || [ "$pid_root_dev:$pid_root_ino" != "$root_identity" ]; then
        if [ "$pid_root_dev_count" -ne 0 ] \
          || [ "$pid_root_ino_count" -ne 0 ] \
          || ! owned_supervisor_process "$pid" "$start" "$token" "$name" "$script_path" "$root_arg" "$state_arg" "$loop_marker" 0 "$containment"; then
          echo "authenticated $name watcher pidfile has no matching launch root identity; refusing replacement" >&2
          return 1
        fi
      elif [ "$pid_root_dev_count" -ne 1 ] || [ "$pid_root_ino_count" -ne 1 ]; then
        echo "authenticated $name watcher pidfile has no matching launch root identity; refusing replacement" >&2
        return 1
      fi
      if ! owned_supervisor_process "$pid" "$start" "$token" "$name" "$script_path" "$root_arg" "$state_arg" "$loop_marker" 0 "$containment"; then
        echo "authenticated $name watcher pidfile belongs to a different or unresolved root; refusing replacement" >&2
        return 1
      fi
      require_root_identity
      if ! stop_owned_supervisor "$name" "$pid" "$start" "$token" "$script_path" "$root_arg" "$state_arg" "$loop_marker" 0 "$containment"; then
        echo "authenticated $name watcher supervisor tree is still live; refusing replacement" >&2
        return 1
      fi
    else
      echo "stale $name watcher pidfile points at unowned pid $pid; ignoring" >&2
    fi
  fi
  require_root_identity
  rm -f "$file"
  if [ "$name" = pending ] && [[ "$token" =~ ^[0-9a-f]{32}$ ]]; then
    rm -f "$state_dir/.pending-ready.$token"
  fi
}

legacy_supervisor_process() {
  local pid="$1" name="$2" script_path="$3" root_arg="$4" state_arg="${5:-}" exe session_id script_index launch_pid_file
  local -a argv=()
  [ -r "/proc/$pid/cmdline" ] || return 1
  mapfile -d '' -t argv <"/proc/$pid/cmdline" || return 1
  exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)"
  session_id="$(process_session_id "$pid")" || return 1
  [ "$session_id" = "$pid" ] || return 1
  [ "${exe##*/}" = bash ] || return 1
  [ "${argv[1]:-}" = -c ] || return 1
  [ "${argv[3]:-}" = "$name-watch-supervisor" ] || return 1
  launch_pid_file="${argv[4]:-}"
  if [ -n "${argv[5]:-}" ] \
    && [ "$launch_pid_file" != "$script_path" ] \
    && { [ "$name" = email ] || [ "$launch_pid_file" = "$state_dir/.$name-supervisor.${argv[5]}.pid" ]; }; then
    if [ "${argv[6]:-}" = "$script_path" ]; then
      script_index=6
    elif [ "${argv[6]:-}" = uv ] \
      && [ "${argv[7]:-}" = run ] \
      && [ "${argv[8]:-}" = --project ] \
      && [ "${argv[9]:-}" = "$helper_dir" ] \
      && [ "${argv[10]:-}" = "$script_path" ]; then
      script_index=10
    else
      return 1
    fi
  elif [ "${argv[4]:-}" = "$script_path" ]; then
    script_index=4
  elif [ "${argv[4]:-}" = uv ] \
    && [ "${argv[5]:-}" = run ] \
    && [ "${argv[6]:-}" = --project ] \
    && [ "${argv[7]:-}" = "$helper_dir" ] \
    && [ "${argv[8]:-}" = "$script_path" ]; then
    script_index=8
  else
    return 1
  fi
  [ "${argv[script_index + 1]:-}" = --root ] || return 1
  same_resolved_path "${argv[script_index + 2]:-}" "$root_arg" || return 1
  if [ "$name" = email ]; then
    cmdline_has_resolved_path_arg_pair "$pid" --mail-dir "$mail_dir" || return 1
  elif [ -n "$state_arg" ]; then
    cmdline_has_arg_pair "$pid" --state-dir "$state_arg"
  fi
}

stop_legacy_supervisors() {
  local name="$1" script_path="$2" root_arg="$3" state_arg="${4:-}" pid
  while read -r pid; do
    [ -n "$pid" ] || continue
    if cmdline_has_arg "$pid" "$name-watch-supervisor" \
      && legacy_supervisor_process "$pid" "$name" "$script_path" "$root_arg" "$state_arg"; then
      require_root_identity
      echo "stopping legacy $name watcher supervisor pid=$pid"
      stop_process_tree "$pid"
    fi
  done < <(pgrep -f -- "$name-watch-supervisor" 2>/dev/null || true)
}

wait_supervised_child() {
  local name="$1" pid="$2" script_path="$3" timeout_s="$4" log_path="$5"
  local deadline_s
  deadline_s=$((SECONDS + timeout_s))
  while [ "$SECONDS" -le "$deadline_s" ]; do
    if ! process_alive "$pid"; then
      echo "$name watcher supervisor exited; see $log_path" >&2
      return 1
    fi
    if supervisor_has_watcher_descendant "$pid" "$script_path"; then
      return 0
    fi
    sleep 0.2
  done
  echo "$name watcher did not start ${script_path##*/} under supervisor pid $pid; see $log_path" >&2
  return 1
}

pending_watcher_is_ready() (
  local supervisor_pid="$1" script_path="$2" ready_file="$3" root_arg="$4" expected_root_identity="$5"
  local ready_pid ready_root ready_version ready_mode ready_uid ready_dev ready_ino root_dev root_ino ready_contents
  local root_fd ready_fd ready_path_dev ready_path_ino ready_fd_dev ready_fd_ino lexical_root_arg
  lexical_root_arg="$(lexical_root_path "$root_arg")"
  [ ! -L "$lexical_root_arg" ] || return 1
  [ -f "$ready_file" ] && [ ! -L "$ready_file" ] || return 1
  exec {root_fd}<"$lexical_root_arg" || return 1
  exec {ready_fd}<"$ready_file" || return 1
  ready_mode="$(stat -Lc '%a' "/proc/self/fd/$ready_fd" 2>/dev/null || true)"
  ready_uid="$(stat -Lc '%u' "/proc/self/fd/$ready_fd" 2>/dev/null || true)"
  [ "$ready_mode" = 600 ] && [ "$ready_uid" = "$(id -u)" ] || return 1
  ready_contents="$(cat <&$ready_fd 2>/dev/null || true)"
  ready_version="$(sed -n 's/^version=//p' <<< "$ready_contents" | sed -n '1p')"
  ready_pid="$(sed -n 's/^pid=//p' <<< "$ready_contents" | sed -n '1p')"
  ready_root="$(sed -n 's/^root=//p' <<< "$ready_contents" | sed -n '1p')"
  ready_dev="$(sed -n 's/^root_dev=//p' <<< "$ready_contents" | sed -n '1p')"
  ready_ino="$(sed -n 's/^root_ino=//p' <<< "$ready_contents" | sed -n '1p')"
  [ "$ready_version" = omo-pending-watch-ready-v1 ] || return 1
  case "$ready_pid" in ''|*[!0-9]*) return 1 ;; esac
  case "$ready_dev" in ''|*[!0-9]*) return 1 ;; esac
  case "$ready_ino" in ''|*[!0-9]*) return 1 ;; esac
  [ "$ready_dev:$ready_ino" = "$expected_root_identity" ] || return 1
  same_resolved_path "$ready_root" "$root_arg" || return 1
  [ ! -L "$lexical_root_arg" ] || return 1
  root_dev="$(stat -Lc '%d' "/proc/self/fd/$root_fd" 2>/dev/null || true)"
  root_ino="$(stat -Lc '%i' "/proc/self/fd/$root_fd" 2>/dev/null || true)"
  [ "$ready_dev" = "$root_dev" ] && [ "$ready_ino" = "$root_ino" ] || return 1
  ready_process_is_active "$ready_pid" || return 1
  watcher_runtime_process "$ready_pid" "$script_path" || return 1
  cmdline_has_arg_pair "$ready_pid" --root "$root_arg" || return 1
  cmdline_has_arg_pair "$ready_pid" --ready-file "$ready_file" || return 1
  process_has_ancestor "$ready_pid" "$supervisor_pid" || return 1
  [ ! -L "$lexical_root_arg" ] || return 1
  root_path_dev="$(stat -c '%d' "$lexical_root_arg" 2>/dev/null || true)"
  root_path_ino="$(stat -c '%i' "$lexical_root_arg" 2>/dev/null || true)"
  [ "$root_path_dev" = "$root_dev" ] && [ "$root_path_ino" = "$root_ino" ] || return 1
  ready_fd_dev="$(stat -Lc '%d' "/proc/self/fd/$ready_fd" 2>/dev/null || true)"
  ready_fd_ino="$(stat -Lc '%i' "/proc/self/fd/$ready_fd" 2>/dev/null || true)"
  ready_path_dev="$(stat -c '%d' "$ready_file" 2>/dev/null || true)"
  ready_path_ino="$(stat -c '%i' "$ready_file" 2>/dev/null || true)"
  [ "$ready_fd_dev" = "$ready_path_dev" ] && [ "$ready_fd_ino" = "$ready_path_ino" ] || return 1
  return 0
)

wait_pending_ready() {
  local pid="$1" script_path="$2" timeout_s="$3" log_path="$4" ready_file="$5" root_arg="$6" expected_root_identity="$7"
  local deadline_s
  deadline_s=$((SECONDS + timeout_s))
  while [ "$SECONDS" -le "$deadline_s" ]; do
    if ! process_alive "$pid"; then
      echo "pending watcher supervisor exited; see $log_path" >&2
      return 1
    fi
    if pending_watcher_is_ready "$pid" "$script_path" "$ready_file" "$root_arg" "$expected_root_identity"; then
      rm -f "$ready_file"
      return 0
    fi
    sleep 0.2
  done
  echo "pending watcher did not become ready after its initial watch tree and inventory; see $log_path" >&2
  return 1
}

wait_launch_pid() {
  local name="$1" launch_pid_file="$2" pid reported_start reported_session
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if [ -s "$launch_pid_file" ]; then
      pid="$(sed -n '1p' "$launch_pid_file")"
      case "$pid" in ''|*[!0-9]*) sleep 0.1; continue ;; esac
      reported_start="$(process_start_ticks "$pid" 2>/dev/null || true)"
      reported_session="$(process_session_id "$pid" 2>/dev/null || true)"
      if [ -n "$reported_start" ] \
        && [ "$reported_session" = "$pid" ] \
        && process_alive "$pid" \
        && cmdline_has_arg "$pid" "$name-watch-supervisor" \
        && cmdline_has_arg "$pid" omo-watcher-subreaper-v1 \
        && cmdline_has_resolved_path_arg "$pid" "$launch_pid_file"; then
        printf '%s\n' "$pid"
        return 0
      fi
    fi
    sleep 0.1
  done
  require_root_identity
  echo "$name watcher supervisor did not report an authenticated pid; retaining launch record and process" >&2
  return 1
}

finalize_launched_supervisor() {
  local name="$1" pid="$2" start="$3" token="$4" script_path="$5" root_arg="$6" state_arg="$7" loop_marker="$8" launch_pid_file="$9"
  if ! owned_supervisor_process "$pid" "$start" "$token" "$name" "$script_path" "$root_arg" "$state_arg" "$loop_marker" 0 subreaper-v1; then
    echo "$name watcher supervisor identity changed before launch record deletion; retaining recovery records" >&2
    return 1
  fi
  require_root_identity
  rm -f "$launch_pid_file"
}

require_root_identity
stop_pidfile_supervisor pending "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status"
stop_legacy_supervisors pending "$helper_dir/omo_pending_watch.py" "$root"
stop_pidfile_supervisor email "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "email watcher exited status"
stop_legacy_supervisors email "$helper_dir/email_idle_watcher.py" "$root" "$state_dir"
stop_pidfile_supervisor guest-hees-email "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "guest-hees email watcher exited status"
stop_pidfile_supervisor audit "$helper_dir/omo_agent_audit.py" "$root" "$state_dir" "audit watcher exited status"
require_root_identity
pending_token="$(owner_token)"
pending_ready_file="$state_dir/.pending-ready.$pending_token"
rm -f "$pending_ready_file"
pending_args=(--root "$root" --ready-file "$pending_ready_file" --expected-root-identity "$root_identity")
pending_launch_pid_file="$state_dir/.pending-supervisor.$pending_token.pid"
require_root_identity
setsid python3 -c "$supervisor_guardian_code" omo-watcher-subreaper-v1 "$pending_launch_pid_file" bash -c '
launch_pid_file="$1"
owner_token="$2"
expected_root_identity="$3"
root_lexical="$4"
shift 4
ready_file="$(dirname "$launch_pid_file")/.pending-ready.$owner_token"
while :; do
  "$@"
  st=$?
  rm -f "$ready_file"
  current_root_identity="$(stat -c "%d:%i" -- "$root_lexical" 2>/dev/null || true)"
  if [ -L "$root_lexical" ] || [ "$current_root_identity" != "$expected_root_identity" ]; then
    printf "%s pending watcher root identity changed; stopping supervisor\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" >&2
    exit 76
  fi
  if [ "$st" -eq 75 ]; then
    printf "%s pending watcher duplicate-root refusal; stopping supervisor\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" >&2
    exit "$st"
  fi
  printf "%s pending watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  sleep 5
done
' pending-watch-supervisor "$pending_launch_pid_file" "$pending_token" "$root_identity" "$root_lexical" "${uv_run[@]}" "$helper_dir/omo_pending_watch.py" "${pending_args[@]}" 8>&- >>"$state_dir/pending-watch.log" 2>&1 &
pending_pid="$(wait_launch_pid pending "$pending_launch_pid_file")"
pending_start="$(process_start_ticks "$pending_pid")"
write_pidfile pending "$pending_pid" "$pending_token" subreaper-v1
finalize_launched_supervisor pending "$pending_pid" "$pending_start" "$pending_token" "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status" "$pending_launch_pid_file" || exit 1
echo "started pending watcher supervisor pid=$pending_pid log=$state_dir/pending-watch.log"
if ! wait_pending_ready "$pending_pid" "$helper_dir/omo_pending_watch.py" "$watcher_health_timeout_s" "$state_dir/pending-watch.log" "$pending_ready_file" "$root" "$root_identity"; then
  rm -f "$pending_ready_file"
  pending_cleanup_ok=0
  if stop_owned_supervisor pending "$pending_pid" "$pending_start" "$pending_token" "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status" 1 subreaper-v1; then
    pending_cleanup_ok=1
  elif ! process_active "$pending_pid"; then
    pending_cleanup_ok=1
  fi
  if [ "$pending_cleanup_ok" -eq 1 ]; then
    wait "$pending_pid" 2>/dev/null || true
    rm -f "$(pid_file pending)"
  else
    echo "pending watcher readiness failed and its authenticated supervisor tree could not be stopped" >&2
  fi
  exit 1
fi
if [ "$start_email" -eq 1 ]; then
  mkdir -p -m 700 "$mail_dir"
  chmod 700 "$mail_dir"
  email_args=(--root "$root" --mail-dir "$mail_dir" --state-dir "$state_dir")
  [ -n "$manager_url" ] && email_args+=(--manager-url "$manager_url")
  [ -n "$manager_target" ] && email_args+=(--manager-target "$manager_target")
  [ -n "$default_contact_agent" ] && email_args+=(--default-contact-agent "$default_contact_agent")
  email_token="$(owner_token)"
  email_launch_pid_file="$state_dir/.email-supervisor.$email_token.pid"
  require_root_identity
  setsid python3 -c "$supervisor_guardian_code" omo-watcher-subreaper-v1 "$email_launch_pid_file" bash -c '
launch_pid_file="$1"
owner_token="$2"
shift 2
startup_grace_s="${OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S:-2}"
started=0
while :; do
  start_s=$SECONDS
  "$@"
  st=$?
  runtime_s=$((SECONDS - start_s))
  printf "%s email watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  if [ "$started" -eq 0 ] && [ "$runtime_s" -lt "$startup_grace_s" ]; then
    exit "$st"
  fi
  started=1
  sleep 5
done
' email-watch-supervisor "$email_launch_pid_file" "$email_token" "${uv_run[@]}" "$helper_dir/email_idle_watcher.py" "${email_args[@]}" 8>&- >>"$state_dir/email-watch.log" 2>&1 &
  email_pid="$(wait_launch_pid email "$email_launch_pid_file")"
  email_start="$(process_start_ticks "$email_pid")"
  write_pidfile email "$email_pid" "$email_token" subreaper-v1
  finalize_launched_supervisor email "$email_pid" "$email_start" "$email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "email watcher exited status" "$email_launch_pid_file" || exit 1
  echo "started email watcher supervisor pid=$email_pid log=$state_dir/email-watch.log mail_dir=$mail_dir"
else
  echo "skipped email watcher; configure the split agent/human email values or enable the legacy email config"
fi
if [ "$start_guest_hees_email" -eq 1 ]; then
  guest_hees_mail_dir="$root/guest_hees_manager_mail"
  mkdir -p -m 700 "$guest_hees_mail_dir"
  chmod 700 "$guest_hees_mail_dir"
  guest_hees_email_args=(--guest-hees --root "$root" --mail-dir "$guest_hees_mail_dir" --state-dir "$state_dir")
  # 🧑 "make sure that in the future replies get sent to the guest also"
  guest_hees_email_lock="$state_dir/source1269-guest-watcher.lock"
  guest_hees_email_token="$(owner_token)"
  guest_hees_email_launch_pid_file="$state_dir/.guest-hees-email-supervisor.$guest_hees_email_token.pid"
  require_root_identity
  setsid python3 -c "$supervisor_guardian_code" omo-watcher-subreaper-v1 "$guest_hees_email_launch_pid_file" bash -c '
launch_pid_file="$1"
owner_token="$2"
lock_file="$3"
shift 3
umask 077
startup_grace_s="${OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S:-2}"
started=0
while :; do
  start_s=$SECONDS
  flock --exclusive --nonblock --conflict-exit-code 75 "$lock_file" "$@"
  st=$?
  runtime_s=$((SECONDS - start_s))
  printf "%s guest-hees email watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  if [ "$st" -eq 75 ]; then
    printf "%s guest-hees email watcher single-instance lock is held; stopping supervisor\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" >&2
    exit "$st"
  fi
  if [ "$started" -eq 0 ] && [ "$runtime_s" -lt "$startup_grace_s" ]; then
    exit "$st"
  fi
  started=1
  sleep 5
done
' guest-hees-email-watch-supervisor "$guest_hees_email_launch_pid_file" "$guest_hees_email_token" "$guest_hees_email_lock" "${uv_run[@]}" "$helper_dir/email_idle_watcher.py" "${guest_hees_email_args[@]}" 8>&- >>"$state_dir/guest-hees-email-watch.log" 2>&1 &
  guest_hees_email_pid="$(wait_launch_pid guest-hees-email "$guest_hees_email_launch_pid_file")"
  guest_hees_email_start="$(process_start_ticks "$guest_hees_email_pid")"
  write_pidfile guest-hees-email "$guest_hees_email_pid" "$guest_hees_email_token" subreaper-v1
  finalize_launched_supervisor guest-hees-email "$guest_hees_email_pid" "$guest_hees_email_start" "$guest_hees_email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "guest-hees email watcher exited status" "$guest_hees_email_launch_pid_file" || exit 1
  echo "started guest-hees email watcher supervisor pid=$guest_hees_email_pid log=$state_dir/guest-hees-email-watch.log"
else
  echo "skipped guest-hees email watcher; approval-gated and disabled by default"
fi
if [ "$start_audit" -eq 1 ]; then
  audit_args=(--root "$root" --state-dir "$state_dir" --loop --enable)
  audit_token="$(owner_token)"
  audit_launch_pid_file="$state_dir/.audit-supervisor.$audit_token.pid"
  require_root_identity
  setsid python3 -c "$supervisor_guardian_code" omo-watcher-subreaper-v1 "$audit_launch_pid_file" bash -c '
launch_pid_file="$1"
owner_token="$2"
shift 2
while :; do
  "$@"
  st=$?
  printf "%s audit watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  sleep 5
done
' audit-watch-supervisor "$audit_launch_pid_file" "$audit_token" "${uv_run[@]}" "$helper_dir/omo_agent_audit.py" "${audit_args[@]}" 8>&- >>"$state_dir/audit-watch.log" 2>&1 &
  audit_pid="$(wait_launch_pid audit "$audit_launch_pid_file")"
  audit_start="$(process_start_ticks "$audit_pid")"
  write_pidfile audit "$audit_pid" "$audit_token" subreaper-v1
  finalize_launched_supervisor audit "$audit_pid" "$audit_start" "$audit_token" "$helper_dir/omo_agent_audit.py" "$root" "$state_dir" "audit watcher exited status" "$audit_launch_pid_file" || exit 1
  echo "started audit watcher supervisor pid=$audit_pid log=$state_dir/audit-watch.log"
else
  echo "skipped agent audit watcher; set OMO_MANAGER_ENABLE_AGENT_AUDIT=true to enable"
fi
if [ "$start_guest_hees_email" -eq 1 ]; then
  if ! wait_supervised_child guest-hees-email "$guest_hees_email_pid" "$helper_dir/email_idle_watcher.py" "$watcher_health_timeout_s" "$state_dir/guest-hees-email-watch.log"; then
    rollback_launched_supervisor guest-hees-email "$guest_hees_email_pid" "$guest_hees_email_start" "$guest_hees_email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "guest-hees email watcher exited status" subreaper-v1 || exit 1
    rollback_launched_supervisor pending "$pending_pid" "$pending_start" "$pending_token" "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status" subreaper-v1 || exit 1
    exit 1
  fi
fi
if [ "$start_email" -eq 1 ]; then
  sleep "$email_supervisor_startup_grace_s"
  if ! wait_supervised_child email "$email_pid" "$helper_dir/email_idle_watcher.py" "$watcher_health_timeout_s" "$state_dir/email-watch.log"; then
    rollback_launched_supervisor email "$email_pid" "$email_start" "$email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "email watcher exited status" subreaper-v1 || exit 1
    if [ "$email_enable" = "auto" ]; then
      echo "email watcher did not stay running in auto mode; continuing without it; see $state_dir/email-watch.log" >&2
    else
      echo "email watcher failed to stay running; see $state_dir/email-watch.log" >&2
      rollback_launched_supervisor pending "$pending_pid" "$pending_start" "$pending_token" "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status" subreaper-v1 || exit 1
      if [ "$start_guest_hees_email" -eq 1 ]; then
        rollback_launched_supervisor guest-hees-email "$guest_hees_email_pid" "$guest_hees_email_start" "$guest_hees_email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "guest-hees email watcher exited status" subreaper-v1 || exit 1
      fi
      if [ "$start_audit" -eq 1 ]; then
        rollback_launched_supervisor audit "$audit_pid" "$audit_start" "$audit_token" "$helper_dir/omo_agent_audit.py" "$root" "$state_dir" "audit watcher exited status" subreaper-v1 || exit 1
      fi
      exit 1
    fi
  fi
fi
if [ "$start_audit" -eq 1 ]; then
  # Audit passes may be deliberately one-shot; the supervisor itself is the
  # health boundary and restarts each bounded invocation.
  if ! owned_supervisor_process "$audit_pid" "$audit_start" "$audit_token" audit "$helper_dir/omo_agent_audit.py" "$root" "$state_dir" "audit watcher exited status" 0 subreaper-v1; then
    rollback_launched_supervisor audit "$audit_pid" "$audit_start" "$audit_token" "$helper_dir/omo_agent_audit.py" "$root" "$state_dir" "audit watcher exited status" subreaper-v1 || exit 1
    echo "agent audit watcher failed to stay running; see $state_dir/audit-watch.log" >&2
    exit 1
  fi
fi
echo "watchers ready"
