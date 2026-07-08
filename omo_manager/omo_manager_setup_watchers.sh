#!/usr/bin/env bash
set -euo pipefail
PATH="$HOME/.config/bin:$PATH"
helper_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
[ -n "$env_manager_url" ] && OMO_MANAGER_URL="${env_manager_url#x}"
[ -n "$env_manager_target" ] && OMO_MANAGER_TMUX_TARGET="${env_manager_target#x}"
[ -n "$env_root" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
[ -n "$env_state_dir" ] && OMO_MANAGER_STATE_DIR="${env_state_dir#x}"
[ -n "$env_mail_dir" ] && OMO_MANAGER_MAIL_DIR="${env_mail_dir#x}"
[ -n "$env_email_enable" ] && OMO_MANAGER_ENABLE_EMAIL_WATCHER="${env_email_enable#x}"
[ -n "$env_email_config" ] && OMO_EMAIL_CONFIG_PATH="${env_email_config#x}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-}"
manager_target="${OMO_MANAGER_TMUX_TARGET:-}"
state_base="${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager"
state_dir="${OMO_MANAGER_STATE_DIR:-$state_base}"
email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER:-auto}"
email_config="${OMO_EMAIL_CONFIG_PATH:-$HOME/.config/himalaya/config.toml}"
mail_dir="${OMO_MANAGER_MAIL_DIR:-$root/manager_mail}"
email_supervisor_startup_grace_s="${OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S:-2}"
export OMO_MANAGER_URL="$manager_url"
export OMO_MANAGER_TMUX_TARGET="$manager_target"
export OMO_WORK_LOGS_ROOT="$root"
export OMO_MANAGER_STATE_DIR="$state_dir"
export OMO_MANAGER_EMAIL_SUPERVISOR_STARTUP_GRACE_S="$email_supervisor_startup_grace_s"
export OMO_MANAGER_MAIL_DIR="$mail_dir"
mkdir -p -m 700 "$state_dir"
chmod 700 "$state_dir"
if [ -z "$manager_url" ] && [ -z "$manager_target" ]; then
  echo "OMO_MANAGER_TMUX_TARGET or OMO_MANAGER_URL is required" >&2
  exit 2
fi
echo "manager_target=${manager_target:-unset} manager_url=${manager_url:-unset}"
pkill -f "[e]mail-watch-supervisor .*--state-dir ${state_dir}" >/dev/null 2>&1 || true
pkill -f "[e]mail_idle_watcher.py .*--state-dir ${state_dir}" >/dev/null 2>&1 || true
pkill -f "[e]mail-watch-supervisor .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "[e]mail_idle_watcher.py" >/dev/null 2>&1 || true
pkill -f "[p]ending-watch-supervisor .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "[o]mo_pending_watch.py .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "[e]mail_idle_watcher.py .*--root ${root}" >/dev/null 2>&1 || true
pending_args=(--root "$root")
setsid bash -c '
while :; do
  "$@"
  st=$?
  printf "%s pending watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  sleep 5
done
' pending-watch-supervisor "${uv_run[@]}" "$helper_dir/omo_pending_watch.py" "${pending_args[@]}" >>"$state_dir/pending-watch.log" 2>&1 &
pending_pid=$!
echo "started pending watcher supervisor pid=$pending_pid log=$state_dir/pending-watch.log"
start_email=0
case "$email_enable" in
  1|true|yes) start_email=1 ;;
  0|false|no) start_email=0 ;;
  auto) [ -f "$email_config" ] && start_email=1 || start_email=0 ;;
  *) echo "OMO_MANAGER_ENABLE_EMAIL_WATCHER must be auto, true, or false" >&2; exit 2 ;;
esac
if [ "$start_email" -eq 1 ]; then
  mkdir -p -m 700 "$mail_dir"
  chmod 700 "$mail_dir"
  email_args=(--root "$root" --mail-dir "$mail_dir" --state-dir "$state_dir")
  [ -n "$manager_url" ] && email_args+=(--manager-url "$manager_url")
  [ -n "$manager_target" ] && email_args+=(--manager-target "$manager_target")
  setsid bash -c '
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
' email-watch-supervisor "${uv_run[@]}" "$helper_dir/email_idle_watcher.py" "${email_args[@]}" >>"$state_dir/email-watch.log" 2>&1 &
  email_pid=$!
  echo "started email watcher supervisor pid=$email_pid log=$state_dir/email-watch.log mail_dir=$mail_dir"
else
  echo "skipped email watcher; set OMO_MANAGER_ENABLE_EMAIL_WATCHER=true and OMO_EMAIL_CONFIG_PATH to enable"
fi
sleep 0.2
kill -0 "$pending_pid" 2>/dev/null || { echo "pending watcher failed to stay running; see $state_dir/pending-watch.log" >&2; exit 1; }
if [ "$start_email" -eq 1 ]; then
  sleep "$email_supervisor_startup_grace_s"
  if ! kill -0 "$email_pid" 2>/dev/null; then
    if [ "$email_enable" = "auto" ]; then
      echo "email watcher did not stay running in auto mode; continuing without it; see $state_dir/email-watch.log" >&2
    else
      echo "email watcher failed to stay running; see $state_dir/email-watch.log" >&2
      exit 1
    fi
  fi
fi
echo "watchers ready"
