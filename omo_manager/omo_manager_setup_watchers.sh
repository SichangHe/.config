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
env_manager_url="${OMO_MANAGER_URL+x}${OMO_MANAGER_URL-}"
env_manager_target="${OMO_MANAGER_TMUX_TARGET+x}${OMO_MANAGER_TMUX_TARGET-}"
env_root="${OMO_WORK_LOGS_ROOT+x}${OMO_WORK_LOGS_ROOT-}"
env_state_dir="${OMO_MANAGER_STATE_DIR+x}${OMO_MANAGER_STATE_DIR-}"
env_mail_dir="${OMO_MANAGER_MAIL_DIR+x}${OMO_MANAGER_MAIL_DIR-}"
env_email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER+x}${OMO_MANAGER_ENABLE_EMAIL_WATCHER-}"
env_email_config="${OMO_EMAIL_CONFIG_PATH+x}${OMO_EMAIL_CONFIG_PATH-}"
env_agent_email="${OMO_AGENT_GMAIL_ADDRESS+x}${OMO_AGENT_GMAIL_ADDRESS-}"
env_agent_password="${OMO_AGENT_GMAIL_APP_PASSWORD+x}${OMO_AGENT_GMAIL_APP_PASSWORD-}"
env_human_email="${OMO_HUMAN_EMAIL_ADDRESS+x}${OMO_HUMAN_EMAIL_ADDRESS-}"
env_human_config="${OMO_HUMAN_EMAIL_CONFIG_PATH+x}${OMO_HUMAN_EMAIL_CONFIG_PATH-}"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
[ -n "${env_manager_url#x}" ] && OMO_MANAGER_URL="${env_manager_url#x}"
[ -n "${env_manager_target#x}" ] && OMO_MANAGER_TMUX_TARGET="${env_manager_target#x}"
[ -n "${env_root#x}" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
[ -n "${env_state_dir#x}" ] && OMO_MANAGER_STATE_DIR="${env_state_dir#x}"
[ -n "${env_mail_dir#x}" ] && OMO_MANAGER_MAIL_DIR="${env_mail_dir#x}"
[ -n "${env_email_enable#x}" ] && OMO_MANAGER_ENABLE_EMAIL_WATCHER="${env_email_enable#x}"
[ -n "${env_email_config#x}" ] && OMO_EMAIL_CONFIG_PATH="${env_email_config#x}"
[ -n "${env_agent_email#x}" ] && OMO_AGENT_GMAIL_ADDRESS="${env_agent_email#x}"
[ -n "${env_agent_password#x}" ] && OMO_AGENT_GMAIL_APP_PASSWORD="${env_agent_password#x}"
[ -n "${env_human_email#x}" ] && OMO_HUMAN_EMAIL_ADDRESS="${env_human_email#x}"
[ -n "${env_human_config#x}" ] && OMO_HUMAN_EMAIL_CONFIG_PATH="${env_human_config#x}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-}"
manager_target="${OMO_MANAGER_TMUX_TARGET:-}"
state_base="${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager"
state_dir="${OMO_MANAGER_STATE_DIR:-$state_base}"
email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER:-auto}"
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
export OMO_MANAGER_URL="$manager_url"
export OMO_MANAGER_TMUX_TARGET="$manager_target"
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

stop_process_tree() {
  local pid="$1"
  local -a descendants=() records=() extra_records=() live=() rescanned=()
  mapfile -t descendants < <(descendant_pids "$pid")
  mapfile -t records < <(record_active_targets "$pid" "${descendants[@]}")
  mapfile -t live < <(known_active_pids "${records[@]}")
  [ "${#live[@]}" -eq 0 ] && return 0
  kill "${live[@]}" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    mapfile -t live < <(known_active_pids "${records[@]}")
    [ "${#live[@]}" -eq 0 ] && return 0
    sleep 0.2
  done
  mapfile -t rescanned < <(
    {
      for target in "${live[@]}"; do
        descendant_pids "$target"
      done
    } | awk 'NF && !seen[$0]++'
  )
  mapfile -t extra_records < <(record_active_targets "${rescanned[@]}")
  records+=("${extra_records[@]}")
  mapfile -t live < <(known_active_pids "${records[@]}")
  [ "${#live[@]}" -eq 0 ] && return 0
  kill -9 "${live[@]}" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5; do
    mapfile -t live < <(known_active_pids "${records[@]}")
    [ "${#live[@]}" -eq 0 ] && return 0
    sleep 0.2
  done
}

write_pidfile() {
  local name="$1" pid="$2" token="$3" file start
  file="$(pid_file "$name")"
  start="$(process_start_ticks "$pid")"
  {
    printf 'pid=%s\n' "$pid"
    printf 'start=%s\n' "$start"
    printf 'token=%s\n' "$token"
  } >"$file"
  chmod 600 "$file"
}

pidfile_value() {
  local file="$1" key="$2"
  sed -n "s/^$key=//p" "$file" 2>/dev/null | sed -n '1p'
}

owned_supervisor_process() {
  local pid="$1" start="$2" token="$3" name="$4" script_path="$5" root_arg="$6" state_arg="${7:-}" loop_marker="${8:-}" current_start
  process_alive "$pid" || return 1
  current_start="$(process_start_ticks "$pid")" || return 1
  [ "$current_start" = "$start" ] || return 1
  cmdline_has_fragment "$pid" "$loop_marker" || return 1
  cmdline_has_arg "$pid" "$name-watch-supervisor" || return 1
  cmdline_has_arg "$pid" "$token" || return 1
  cmdline_has_resolved_path_arg "$pid" "$script_path" || return 1
  cmdline_has_arg_pair "$pid" --root "$root_arg" || return 1
  [ -z "$state_arg" ] || cmdline_has_arg_pair "$pid" --state-dir "$state_arg"
}

stop_owned_supervisor() {
  local name="$1" pid="$2" start="$3" token="$4" script_path="$5" root_arg="$6" state_arg="${7:-}" loop_marker="${8:-}"
  if owned_supervisor_process "$pid" "$start" "$token" "$name" "$script_path" "$root_arg" "$state_arg" "$loop_marker"; then
    stop_process_tree "$pid"
    return 0
  fi
  return 1
}

stop_pidfile_supervisor() {
  local name="$1" script_path="$2" root_arg="$3" state_arg="${4:-}" loop_marker="${5:-}" file pid start token
  file="$(pid_file "$name")"
  if [ ! -r "$file" ]; then
    return 0
  fi
  pid="$(pidfile_value "$file" pid)"
  start="$(pidfile_value "$file" start)"
  token="$(pidfile_value "$file" token)"
  if process_alive "$pid" && ! stop_owned_supervisor "$name" "$pid" "$start" "$token" "$script_path" "$root_arg" "$state_arg" "$loop_marker"; then
    echo "stale $name watcher pidfile points at unowned pid $pid; ignoring" >&2
  fi
  rm -f "$file"
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
  if [ -n "${argv[5]:-}" ] && [ "$launch_pid_file" = "$state_dir/.$name-supervisor.${argv[5]}.pid" ]; then
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
  [ "${argv[script_index + 2]:-}" = "$root_arg" ] || return 1
  [ -z "$state_arg" ] || cmdline_has_arg_pair "$pid" --state-dir "$state_arg"
}

stop_legacy_supervisors() {
  local name="$1" script_path="$2" root_arg="$3" state_arg="${4:-}" pid
  while read -r pid; do
    [ -n "$pid" ] || continue
    if cmdline_has_arg "$pid" "$name-watch-supervisor" \
      && legacy_supervisor_process "$pid" "$name" "$script_path" "$root_arg" "$state_arg"; then
      echo "stopping legacy $name watcher supervisor pid=$pid"
      stop_process_tree "$pid"
    fi
  done < <(ps -eo pid=)
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

wait_launch_pid() {
  local name="$1" launcher_pid="$2" launcher_start="$3" launch_pid_file="$4" current_start pid
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if [ -s "$launch_pid_file" ]; then
      pid="$(sed -n '1p' "$launch_pid_file")"
      if process_alive "$pid"; then
        rm -f "$launch_pid_file"
        printf '%s\n' "$pid"
        return 0
      fi
    fi
    sleep 0.1
  done
  rm -f "$launch_pid_file"
  current_start="$(process_start_ticks "$launcher_pid" 2>/dev/null || true)"
  if [ -n "$launcher_start" ] \
    && [ "$current_start" = "$launcher_start" ] \
    && cmdline_has_arg "$launcher_pid" "$name-watch-supervisor"; then
    stop_process_tree "$launcher_pid"
  fi
  echo "$name watcher supervisor did not report its pid" >&2
  return 1
}

stop_pidfile_supervisor pending "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status"
stop_legacy_supervisors pending "$helper_dir/omo_pending_watch.py" "$root"
stop_pidfile_supervisor email "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "email watcher exited status"
stop_legacy_supervisors email "$helper_dir/email_idle_watcher.py" "$root" "$state_dir"
pending_args=(--root "$root")
pending_token="$(owner_token)"
pending_launch_pid_file="$state_dir/.pending-supervisor.$pending_token.pid"
setsid bash -c '
launch_pid_file="$1"
owner_token="$2"
shift 2
printf "%s\n" "$$" >"$launch_pid_file"
while :; do
  "$@"
  st=$?
  printf "%s pending watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  sleep 5
done
' pending-watch-supervisor "$pending_launch_pid_file" "$pending_token" "${uv_run[@]}" "$helper_dir/omo_pending_watch.py" "${pending_args[@]}" 8>&- >>"$state_dir/pending-watch.log" 2>&1 &
pending_launcher_pid=$!
pending_launcher_start="$(process_start_ticks "$pending_launcher_pid" 2>/dev/null || true)"
pending_pid="$(wait_launch_pid pending "$pending_launcher_pid" "$pending_launcher_start" "$pending_launch_pid_file")"
pending_start="$(process_start_ticks "$pending_pid")"
write_pidfile pending "$pending_pid" "$pending_token"
echo "started pending watcher supervisor pid=$pending_pid log=$state_dir/pending-watch.log"
if [ "$start_email" -eq 1 ]; then
  mkdir -p -m 700 "$mail_dir"
  chmod 700 "$mail_dir"
  email_args=(--root "$root" --mail-dir "$mail_dir" --state-dir "$state_dir")
  [ -n "$manager_url" ] && email_args+=(--manager-url "$manager_url")
  [ -n "$manager_target" ] && email_args+=(--manager-target "$manager_target")
  email_token="$(owner_token)"
  email_launch_pid_file="$state_dir/.email-supervisor.$email_token.pid"
  setsid bash -c '
launch_pid_file="$1"
owner_token="$2"
shift 2
printf "%s\n" "$$" >"$launch_pid_file"
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
  email_launcher_pid=$!
  email_launcher_start="$(process_start_ticks "$email_launcher_pid" 2>/dev/null || true)"
  email_pid="$(wait_launch_pid email "$email_launcher_pid" "$email_launcher_start" "$email_launch_pid_file")"
  email_start="$(process_start_ticks "$email_pid")"
  write_pidfile email "$email_pid" "$email_token"
  echo "started email watcher supervisor pid=$email_pid log=$state_dir/email-watch.log mail_dir=$mail_dir"
else
  echo "skipped email watcher; configure the split agent/human email values or enable the legacy email config"
fi
if ! wait_supervised_child pending "$pending_pid" "$helper_dir/omo_pending_watch.py" "$watcher_health_timeout_s" "$state_dir/pending-watch.log"; then
  if [ "$start_email" -eq 1 ]; then
    stop_owned_supervisor email "$email_pid" "$email_start" "$email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "email watcher exited status" || true
    rm -f "$(pid_file email)"
  fi
  stop_owned_supervisor pending "$pending_pid" "$pending_start" "$pending_token" "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status" || true
  rm -f "$(pid_file pending)"
  exit 1
fi
if [ "$start_email" -eq 1 ]; then
  sleep "$email_supervisor_startup_grace_s"
  if ! wait_supervised_child email "$email_pid" "$helper_dir/email_idle_watcher.py" "$watcher_health_timeout_s" "$state_dir/email-watch.log"; then
    stop_owned_supervisor email "$email_pid" "$email_start" "$email_token" "$helper_dir/email_idle_watcher.py" "$root" "$state_dir" "email watcher exited status" || true
    if [ "$email_enable" = "auto" ]; then
      echo "email watcher did not stay running in auto mode; continuing without it; see $state_dir/email-watch.log" >&2
      rm -f "$(pid_file email)"
    else
      rm -f "$(pid_file email)"
      echo "email watcher failed to stay running; see $state_dir/email-watch.log" >&2
      stop_owned_supervisor pending "$pending_pid" "$pending_start" "$pending_token" "$helper_dir/omo_pending_watch.py" "$root" "" "pending watcher exited status" || true
      rm -f "$(pid_file pending)"
      exit 1
    fi
  fi
fi
echo "watchers ready"
