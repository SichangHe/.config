#!/usr/bin/env bash
#!/usr/bin/env bash
set -euo pipefail
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-http://127.0.0.1:18790}"
state_dir="${OMO_MANAGER_STATE_DIR:-/tmp/omo-manager}"
pending_seen="${OMO_MANAGER_PENDING_SEEN:-/tmp/omo-manager-pending-seen.tsv}"
mkdir -p "$state_dir"
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
nohup python3 "$HOME/.config/omo_manager/omo_pending_watch.py" --root "$root" --manager-url "$manager_url" >"$state_dir/pending-watch.log" 2>&1 &
echo "started pending watcher pid=$! log=$state_dir/pending-watch.log"
nohup python3 "$HOME/.config/omo_manager/email_idle_watcher.py" --root "$root" --manager-url "$manager_url" >"$state_dir/email-watch.log" 2>&1 &
echo "started email watcher pid=$! log=$state_dir/email-watch.log"
sleep 0.2
pgrep -f "omo_pending_watch.py .*--root ${root}" >/dev/null || { echo "pending watcher failed to stay running; see $state_dir/pending-watch.log" >&2; exit 1; }
pgrep -f "email_idle_watcher.py .*--root ${root}" >/dev/null || { echo "email watcher failed to stay running; see $state_dir/email-watch.log" >&2; exit 1; }
