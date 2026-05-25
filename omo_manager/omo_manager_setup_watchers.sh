#!/usr/bin/env bash
#!/usr/bin/env bash
set -euo pipefail
env_manager_url="${OMO_MANAGER_URL+x}${OMO_MANAGER_URL-}"
env_root="${OMO_WORK_LOGS_ROOT+x}${OMO_WORK_LOGS_ROOT-}"
env_state_dir="${OMO_MANAGER_STATE_DIR+x}${OMO_MANAGER_STATE_DIR-}"
env_pending_seen="${OMO_MANAGER_PENDING_SEEN+x}${OMO_MANAGER_PENDING_SEEN-}"
env_mail_dir="${OMO_MANAGER_MAIL_DIR+x}${OMO_MANAGER_MAIL_DIR-}"
env_email_enable="${OMO_MANAGER_ENABLE_EMAIL_WATCHER+x}${OMO_MANAGER_ENABLE_EMAIL_WATCHER-}"
env_email_config="${OMO_EMAIL_CONFIG_PATH+x}${OMO_EMAIL_CONFIG_PATH-}"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
[ -n "$env_manager_url" ] && OMO_MANAGER_URL="${env_manager_url#x}"
[ -n "$env_root" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
[ -n "$env_state_dir" ] && OMO_MANAGER_STATE_DIR="${env_state_dir#x}"
[ -n "$env_pending_seen" ] && OMO_MANAGER_PENDING_SEEN="${env_pending_seen#x}"
[ -n "$env_mail_dir" ] && OMO_MANAGER_MAIL_DIR="${env_mail_dir#x}"
[ -n "$env_email_enable" ] && OMO_MANAGER_ENABLE_EMAIL_WATCHER="${env_email_enable#x}"
[ -n "$env_email_config" ] && OMO_EMAIL_CONFIG_PATH="${env_email_config#x}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-http://127.0.0.1:18790}"
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
export OMO_MANAGER_MAIL_DIR="$mail_dir"
mkdir -p -m 700 "$state_dir"
chmod 700 "$state_dir"
python3 - "$manager_url" <<'PY'
from __future__ import annotations
import json
import sys
import urllib.request
from urllib.parse import urlparse
url = urlparse(sys.argv[1])
host = url.hostname
port = url.port
if host is None or port is None:
    raise SystemExit(f"manager URL must include host and port: {sys.argv[1]}")
base_url = sys.argv[1].rstrip("/")
try:
    with urllib.request.urlopen(f"{base_url}/global/health", timeout=3) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except Exception as exc:
    raise SystemExit(f"manager endpoint does not expose OpenCode server API at {sys.argv[1]}: {exc}") from exc
if not body.get("healthy"):
    raise SystemExit(f"manager endpoint is unhealthy at {sys.argv[1]}: {body}")
req = urllib.request.Request(
    f"{base_url}/tui/append-prompt",
    data=json.dumps({"text": ""}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=3) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}")
except Exception as exc:
    raise SystemExit(f"manager endpoint cannot append TUI prompts at {sys.argv[1]}: {exc}") from exc
PY
echo "manager_url=$manager_url"
pkill -f "omo_pending_watch.py .*--root ${root}" >/dev/null 2>&1 || true
pkill -f "email_idle_watcher.py .*--root ${root}" >/dev/null 2>&1 || true
rm -f "$pending_seen"
nohup python3 "$HOME/.config/omo_manager/omo_pending_watch.py" --root "$root" --manager-url "$manager_url" --state "$pending_seen" >"$state_dir/pending-watch.log" 2>&1 &
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
  nohup python3 "$HOME/.config/omo_manager/email_idle_watcher.py" --root "$root" --manager-url "$manager_url" --mail-dir "$mail_dir" --state-dir "$state_dir" >"$state_dir/email-watch.log" 2>&1 &
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
