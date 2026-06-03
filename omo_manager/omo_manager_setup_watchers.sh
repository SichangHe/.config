#!/usr/bin/env bash
set -euo pipefail
PATH="$HOME/.config/bin:$PATH"
env_manager_url="${OMO_MANAGER_URL+x}${OMO_MANAGER_URL-}"
env_manager_target="${OMO_MANAGER_TMUX_TARGET+x}${OMO_MANAGER_TMUX_TARGET-}"
env_root="${OMO_WORK_LOGS_ROOT+x}${OMO_WORK_LOGS_ROOT-}"
env_state_dir="${OMO_MANAGER_STATE_DIR+x}${OMO_MANAGER_STATE_DIR-}"
env_pending_seen="${OMO_MANAGER_PENDING_SEEN+x}${OMO_MANAGER_PENDING_SEEN-}"
env_mail_dir="${OMO_MANAGER_MAIL_DIR+x}${OMO_MANAGER_MAIL_DIR-}"
env_active_log="${OMO_MANAGER_ACTIVE_LOG+x}${OMO_MANAGER_ACTIVE_LOG-}"
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
[ -n "$env_pending_seen" ] && OMO_MANAGER_PENDING_SEEN="${env_pending_seen#x}"
[ -n "$env_mail_dir" ] && OMO_MANAGER_MAIL_DIR="${env_mail_dir#x}"
[ -n "$env_active_log" ] && OMO_MANAGER_ACTIVE_LOG="${env_active_log#x}"
[ -n "$env_email_enable" ] && OMO_MANAGER_ENABLE_EMAIL_WATCHER="${env_email_enable#x}"
[ -n "$env_email_config" ] && OMO_EMAIL_CONFIG_PATH="${env_email_config#x}"
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
email_config="${OMO_EMAIL_CONFIG_PATH:-$HOME/.config/himalaya/config.toml}"
mail_dir="${OMO_MANAGER_MAIL_DIR:-$root/manager_mail}"
active_log="${OMO_MANAGER_ACTIVE_LOG:-$root/work_manager_$(date +%F).md}"
export OMO_MANAGER_MAIL_DIR="$mail_dir"
mkdir -p -m 700 "$state_dir"
chmod 700 "$state_dir"
if [ -z "$manager_url" ] && [ -z "$manager_target" ]; then
  echo "OMO_MANAGER_TMUX_TARGET or OMO_MANAGER_URL is required" >&2
  exit 2
fi
echo "manager_target=${manager_target:-unset} manager_url=${manager_url:-unset}"
pkill -f "omo_pending_watch.py .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "email_idle_watcher.py .*--root ${root}" >/dev/null 2>&1 || true
rm -f "$pending_seen"
pending_args=(--root "$root" --state "$pending_seen")
[ -n "$manager_target" ] && pending_args+=(--manager-target "$manager_target")
[ -n "$manager_url" ] && pending_args+=(--manager-url "$manager_url")
nohup omo_pending_watch.py "${pending_args[@]}" >"$state_dir/pending-watch.log" 2>&1 &
echo "started pending watcher pid=$! log=$state_dir/pending-watch.log"
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
  email_args=(--root "$root" --mail-dir "$mail_dir" --state-dir "$state_dir" --manager-file "$active_log")
  [ -n "$manager_url" ] && email_args+=(--manager-url "$manager_url")
  nohup email_idle_watcher.py "${email_args[@]}" >"$state_dir/email-watch.log" 2>&1 &
  echo "started email watcher pid=$! log=$state_dir/email-watch.log mail_dir=$mail_dir"
else
  echo "skipped email watcher; set OMO_MANAGER_ENABLE_EMAIL_WATCHER=true and OMO_EMAIL_CONFIG_PATH to enable"
fi
sleep 0.2
pgrep -f "omo_pending_watch.py .*--root ${root}" >/dev/null || { echo "pending watcher failed to stay running; see $state_dir/pending-watch.log" >&2; exit 1; }
if [ "$start_email" -eq 1 ]; then
  if ! pgrep -f "email_idle_watcher.py .*--root ${root}" >/dev/null; then
    if [ "$email_enable" = "auto" ]; then
      echo "email watcher did not stay running in auto mode; continuing without it; see $state_dir/email-watch.log" >&2
    else
      echo "email watcher failed to stay running; see $state_dir/email-watch.log" >&2
      exit 1
    fi
  fi
fi
echo "watchers ready"
