#!/usr/bin/env bash
set -euo pipefail

local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi

manager_url="${OMO_MANAGER_URL:-http://127.0.0.1:18790}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
state_dir="${OMO_MANAGER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager}"
workdir="${OMO_MANAGER_WORKDIR:-$HOME/.config}"
tmux_target="${OMO_MANAGER_TMUX_TARGET:-}"
manager_model="${OMO_MANAGER_MODEL:-openai/gpt-5.6-terra}"

usage() {
  cat <<'EOF'
Usage: omo_manager_watchdog.sh [--manager-url URL] [--root DIR] [--state-dir DIR] [--workdir DIR] [--tmux-target TARGET] [--model MODEL]

Single-shot manager health check. If unhealthy, it writes an actionable recovery
signal and exits non-zero without looping or mutating the manager TUI.
EOF
}

need_value() {
  if [ "$#" -lt 2 ]; then
    echo "$1 requires a value" >&2
    usage >&2
    exit 2
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --manager-url) need_value "$@"; manager_url="$2"; shift 2 ;;
    --root) need_value "$@"; root="$2"; shift 2 ;;
    --state-dir) need_value "$@"; state_dir="$2"; shift 2 ;;
    --workdir) need_value "$@"; workdir="$2"; shift 2 ;;
    --tmux-target) need_value "$@"; tmux_target="$2"; shift 2 ;;
    --model) need_value "$@"; manager_model="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

mkdir -p -m 700 "$state_dir"
chmod 700 "$state_dir"
signal_file="$state_dir/manager-recovery-needed.txt"

manager_endpoint() {
  python3 - "$manager_url" <<'PY'
from __future__ import annotations
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1].rstrip("/"))
if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
    raise SystemExit(f"manager URL must be http://host:port: {sys.argv[1]}")
print(parsed.hostname)
print(parsed.port)
PY
}

health_check() {
  python3 - "$manager_url" "$root" <<'PY'
from __future__ import annotations
import http.client
import json
import sys
import urllib.parse

base_url = sys.argv[1].rstrip("/")
root = sys.argv[2]


def post_json(url: str, payload: object) -> None:
    data = json.dumps(payload).encode("utf-8")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname is None:
        raise ValueError(f"unsupported manager URL: {url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=3)
    try:
        conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace").strip()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from {url}: {body}")
    finally:
        conn.close()
    if body not in {"true", ""}:
        raise RuntimeError(f"unexpected response from {url}: {body}")


def endpoint(route: str) -> str:
    query = urllib.parse.urlencode({"directory": root})
    return f"{base_url}{route}?{query}"


def check_event_stream(host: str, port: int) -> None:
    conn = http.client.HTTPConnection(host, port, timeout=3)
    try:
        conn.request("GET", "/event")
        resp = conn.getresponse()
        if resp.status >= 400:
            body = resp.read(200).decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {resp.status} from /event: {body}")
    finally:
        conn.close()


try:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
        raise ValueError(f"manager URL must be http://host:port: {base_url}")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
    try:
        conn.request("GET", "/global/health")
        resp = conn.getresponse()
        body = json.loads(resp.read().decode("utf-8"))
    finally:
        conn.close()
    if not body.get("healthy"):
        raise RuntimeError(f"unhealthy body: {body}")
    check_event_stream(parsed.hostname, parsed.port)
    post_json(endpoint("/tui/append-prompt"), {"text": ""})
except Exception as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
PY
}

write_signal() {
  local reason="$1"
  local host_port
  if ! host_port=$(manager_endpoint 2>/dev/null); then
    host_port=$'127.0.0.1\n18790'
  fi
  local manager_host manager_port
  manager_host=$(printf '%s\n' "$host_port" | sed -n '1p')
  manager_port=$(printf '%s\n' "$host_port" | sed -n '2p')
  cat >"$signal_file" <<EOF
manager watchdog recovery needed
time: $(date '+%Y-%m-%d %H:%M:%S %z')
reason: $reason
manager-url: $manager_url
root: $root
workdir: $workdir
tmux-target: ${tmux_target:-unset}
restart-command: start or switch to a shell in the manager pane, then run: cd $(printf '%q' "$workdir") && opencode --port $manager_port --hostname $manager_host --model $(printf '%q' "$manager_model") .
watcher-refresh: ~/.config/omo_manager/omo_manager_setup_watchers.sh
EOF
  chmod 600 "$signal_file"
}

if health_check >/dev/null 2>"$state_dir/manager-watchdog-last-error.log"; then
  rm -f "$signal_file"
  echo "ok: manager healthy at $manager_url"
  exit 0
fi

reason=$(tr '\n' ' ' <"$state_dir/manager-watchdog-last-error.log" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')
reason=${reason:-health check failed}
write_signal "$reason"
echo "unhealthy: $reason; wrote $signal_file" >&2
exit 2
