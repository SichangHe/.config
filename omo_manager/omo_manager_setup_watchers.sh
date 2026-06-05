#!/usr/bin/env bash
set -euo pipefail
PATH="$HOME/.config/bin:$PATH"
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
env_pending_seen="${OMO_MANAGER_PENDING_SEEN+x}${OMO_MANAGER_PENDING_SEEN-}"
env_mail_dir="${OMO_MANAGER_MAIL_DIR+x}${OMO_MANAGER_MAIL_DIR-}"
env_email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER+x}${OMO_MANAGER_ENABLE_EMAIL_WATCHER-}"
env_email_config="${OMO_EMAIL_CONFIG_PATH+x}${OMO_EMAIL_CONFIG_PATH-}"
env_stuck_enable="${OMO_MANAGER_ENABLE_STUCK_WATCHER+x}${OMO_MANAGER_ENABLE_STUCK_WATCHER-}"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
[ -n "$env_manager_url" ] && OMO_MANAGER_URL="${env_manager_url#x}"
[ -n "$env_manager_target" ] && OMO_MANAGER_TMUX_TARGET="${env_manager_target#x}"
[ -n "$env_root" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
[ -n "$env_state_dir" ] && OMO_MANAGER_STATE_DIR="${env_state_dir#x}"
[ -n "$env_pending_seen" ] && OMO_MANAGER_PENDING_SEEN="${env_pending_seen#x}"
[ -n "$env_mail_dir" ] && OMO_MANAGER_MAIL_DIR="${env_mail_dir#x}"
[ -n "$env_email_enable" ] && OMO_MANAGER_ENABLE_EMAIL_WATCHER="${env_email_enable#x}"
[ -n "$env_email_config" ] && OMO_EMAIL_CONFIG_PATH="${env_email_config#x}"
[ -n "$env_stuck_enable" ] && OMO_MANAGER_ENABLE_STUCK_WATCHER="${env_stuck_enable#x}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-}"
manager_target="${OMO_MANAGER_TMUX_TARGET:-}"
state_base="${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager"
state_dir="${OMO_MANAGER_STATE_DIR:-$state_base}"
pending_seen="${OMO_MANAGER_PENDING_SEEN:-$state_dir/pending-seen.tsv}"
if [ "$pending_seen" = "/tmp/omo-manager-pending-seen.tsv" ]; then
  pending_seen="$state_dir/pending-seen.tsv"
fi
export OMO_MANAGER_PENDING_SEEN="$pending_seen"
email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER:-auto}"
stuck_enable="${OMO_MANAGER_ENABLE_STUCK_WATCHER:-true}"
email_config="${OMO_EMAIL_CONFIG_PATH:-$HOME/.config/himalaya/config.toml}"
mail_dir="${OMO_MANAGER_MAIL_DIR:-$root/manager_mail}"
export OMO_MANAGER_MAIL_DIR="$mail_dir"
mkdir -p -m 700 "$state_dir"
chmod 700 "$state_dir"
if [ -z "$manager_url" ] && [ -z "$manager_target" ]; then
  echo "OMO_MANAGER_TMUX_TARGET or OMO_MANAGER_URL is required" >&2
  exit 2
fi
echo "manager_target=${manager_target:-unset} manager_url=${manager_url:-unset}"
pkill -f "[p]ending-watch-supervisor .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "[o]mo_pending_watch.py .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "[e]mail_idle_watcher.py .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "[o]mo_stuck_watch.py .*--watch" >/dev/null 2>&1 || true
pending_args=(--root "$root" --state "$pending_seen")
[ -n "$manager_target" ] && pending_args+=(--manager-target "$manager_target")
[ -n "$manager_url" ] && pending_args+=(--manager-url "$manager_url")
setsid bash -c '
while :; do
  "$@"
  st=$?
  printf "%s pending watcher exited status=%s; restarting in 5s\n" "$(date "+%Y-%m-%d %H:%M:%S %z")" "$st" >&2
  sleep 5
done
' pending-watch-supervisor omo_pending_watch.py "${pending_args[@]}" >"$state_dir/pending-watch.log" 2>&1 &
pending_pid=$!
echo "started pending watcher supervisor pid=$pending_pid log=$state_dir/pending-watch.log"
stuck_pid=""
case "$stuck_enable" in
  1|true|yes)
    setsid omo_stuck_watch.py --watch --interval-s "${OMO_MANAGER_STUCK_INTERVAL_S:-60}" --stale-after-s "${OMO_MANAGER_STUCK_STALE_AFTER_S:-900}" --max-iterations "${OMO_MANAGER_STUCK_MAX_ITERATIONS:-10000}" >"$state_dir/stuck-watch.log" 2>&1 &
    stuck_pid=$!
    echo "started stuck watcher pid=$stuck_pid log=$state_dir/stuck-watch.log"
    ;;
  0|false|no) echo "skipped stuck watcher; OMO_MANAGER_ENABLE_STUCK_WATCHER=false" ;;
  *) echo "OMO_MANAGER_ENABLE_STUCK_WATCHER must be true or false" >&2; exit 2 ;;
esac
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
  setsid email_idle_watcher.py "${email_args[@]}" >"$state_dir/email-watch.log" 2>&1 &
  email_pid=$!
  echo "started email watcher pid=$email_pid log=$state_dir/email-watch.log mail_dir=$mail_dir"
else
  echo "skipped email watcher; set OMO_MANAGER_ENABLE_EMAIL_WATCHER=true and OMO_EMAIL_CONFIG_PATH to enable"
fi
sleep 0.2
kill -0 "$pending_pid" 2>/dev/null || { echo "pending watcher failed to stay running; see $state_dir/pending-watch.log" >&2; exit 1; }
if [ -n "$stuck_pid" ]; then
  kill -0 "$stuck_pid" 2>/dev/null || { echo "stuck watcher failed to stay running; see $state_dir/stuck-watch.log" >&2; exit 1; }
fi
if [ "$start_email" -eq 1 ]; then
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
