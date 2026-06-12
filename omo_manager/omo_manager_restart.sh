#!/usr/bin/env bash
set -euo pipefail

env_manager_url="${OMO_MANAGER_URL+x}${OMO_MANAGER_URL-}"
env_root="${OMO_WORK_LOGS_ROOT+x}${OMO_WORK_LOGS_ROOT-}"
env_state_dir="${OMO_MANAGER_STATE_DIR+x}${OMO_MANAGER_STATE_DIR-}"
env_workdir="${OMO_MANAGER_WORKDIR+x}${OMO_MANAGER_WORKDIR-}"
env_tmux_target="${OMO_MANAGER_TMUX_TARGET+x}${OMO_MANAGER_TMUX_TARGET-}"
env_manager_model="${OMO_MANAGER_MODEL+x}${OMO_MANAGER_MODEL-}"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
[ -n "$env_manager_url" ] && OMO_MANAGER_URL="${env_manager_url#x}"
[ -n "$env_root" ] && OMO_WORK_LOGS_ROOT="${env_root#x}"
[ -n "$env_state_dir" ] && OMO_MANAGER_STATE_DIR="${env_state_dir#x}"
[ -n "$env_workdir" ] && OMO_MANAGER_WORKDIR="${env_workdir#x}"
[ -n "$env_tmux_target" ] && OMO_MANAGER_TMUX_TARGET="${env_tmux_target#x}"
[ -n "$env_manager_model" ] && OMO_MANAGER_MODEL="${env_manager_model#x}"

manager_url="${OMO_MANAGER_URL:-http://127.0.0.1:18790}"
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
state_dir="${OMO_MANAGER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager}"
workdir="${OMO_MANAGER_WORKDIR:-$root}"
tmux_target="${OMO_MANAGER_TMUX_TARGET:-omo-manager:0.0}"
manager_model="${OMO_MANAGER_MODEL:-openai/gpt-5.4}"
startup_prompt=1
refresh_watchers=1
dry_run=0
force_port=0
lock_fd=9

usage() {
  cat <<'EOF'
Usage: omo_manager_restart.sh [options]

Safely replace the OpenCode manager TUI in tmux, then refresh manager watchers.

Options:
  --manager-url URL       Manager OpenCode URL (default: OMO_MANAGER_URL or http://127.0.0.1:18790)
  --root DIR              Markdown work-log root (default: OMO_WORK_LOGS_ROOT or ~/work_logs)
  --state-dir DIR         Private state/log dir (default: OMO_MANAGER_STATE_DIR or ~/.local/state/omo-manager)
  --workdir DIR           Directory where manager OpenCode should run (default: root)
  --tmux-target TARGET    Existing pane or manager session pane (default: OMO_MANAGER_TMUX_TARGET or omo-manager:0.0)
  --model MODEL           OpenCode model for the manager (default: OMO_MANAGER_MODEL or openai/gpt-5.4)
  --no-startup-prompt     Do not submit the post-restart work-log MANAGER.md prompt
  --no-refresh-watchers   Do not run omo_manager_setup_watchers.sh after health succeeds
  --force-port            Kill any non-manager listener still occupying the manager port after Ctrl-C
  --dry-run               Print planned actions without changing tmux/processes/watchers
  -h, --help              Show this help

Examples:
  OMO_MANAGER_URL=http://127.0.0.1:18917 omo_manager_restart.sh --tmux-target wl:4.0
  omo_manager_restart.sh --manager-url http://127.0.0.1:18790 --tmux-target omo-manager:0.0
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
    --no-startup-prompt) startup_prompt=0; shift ;;
    --no-refresh-watchers) refresh_watchers=0; shift ;;
    --force-port) force_port=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

manager_endpoint() {
  python3 - "$manager_url" <<'PY'
from __future__ import annotations
import sys
import urllib.parse

parsed = urllib.parse.urlparse(sys.argv[1].rstrip("/"))
if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
    raise SystemExit(f"manager URL must be http://host:port: {sys.argv[1]}")
if parsed.hostname not in {"127.0.0.1", "localhost"}:
    raise SystemExit(f"manager URL must use a loopback host: {sys.argv[1]}")
print(parsed.hostname)
print(parsed.port)
PY
}

host_port=$(manager_endpoint)
manager_host=$(printf '%s\n' "$host_port" | sed -n '1p')
manager_port=$(printf '%s\n' "$host_port" | sed -n '2p')
base_url="http://${manager_host}:${manager_port}"
restart_id="$(date '+%Y%m%d-%H%M%S')"
log_dir="$state_dir/restarts"
pane_log="$log_dir/manager-pane-before-$restart_id.log"
restart_log="$log_dir/restart-$restart_id.log"

run() {
  printf '+ %q' "$1"
  shift || true
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
  if [ "$dry_run" -eq 0 ]; then
    "$@"
  fi
}

log() {
  printf '%s\n' "$*" | tee -a "$restart_log"
}

port_listener_pids() {
  ss -ltnp "sport = :$manager_port" 2>/dev/null \
    | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
    | sort -u
}

tmux_target_pid() {
  tmux display-message -p -t "$tmux_target" '#{pane_pid}' 2>/dev/null || true
}

process_field() {
  local pid="$1"
  local field="$2"
  ps -p "$pid" -o "$field"= 2>/dev/null | tr -d ' '
}

process_is_descendant_of() {
  local pid="$1"
  local ancestor="$2"
  local current="$pid"
  [ -n "$current" ] && [ -n "$ancestor" ] || return 1
  while [ -n "$current" ] && [ "$current" != "1" ]; do
    if [ "$current" = "$ancestor" ]; then
      return 0
    fi
    current="$(process_field "$current" ppid)"
  done
  return 1
}

describe_port_listener() {
  local pids
  pids="$(port_listener_pids)"
  if [ -z "$pids" ]; then
    echo "port $manager_port: no listener"
    return 0
  fi
  echo "port $manager_port listener pids: $pids"
  for pid in $pids; do
    ps -p "$pid" -o pid=,ppid=,pgid=,comm=,args= 2>/dev/null || true
  done
}

port_occupied() {
  python3 - "$manager_host" "$manager_port" <<'PY'
from __future__ import annotations
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.2)
    raise SystemExit(0 if sock.connect_ex((host, port)) == 0 else 1)
PY
}

wait_port_free() {
  local deadline=$((SECONDS + 10))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if ! port_occupied; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

wait_pane_ready() {
  local deadline=$((SECONDS + 10))
  local cmd
  while [ "$SECONDS" -lt "$deadline" ]; do
    cmd="$(tmux display-message -p -t "$tmux_target" '#{pane_current_command}' 2>/dev/null || true)"
    case "$cmd" in
      opencode|node|bun) sleep 0.25 ;;
      bash|dash|fish|sh|zsh) return 0 ;;
      *) sleep 0.25 ;;
    esac
  done
  return 1
}

pane_restartable() {
  local cmd
  cmd="$(tmux display-message -p -t "$tmux_target" '#{pane_current_command}' 2>/dev/null || true)"
  case "$cmd" in
    bash|dash|fish|opencode|sh|zsh) return 0 ;;
    node|bun) port_occupied && port_owned_by_target ;;
    *) return 1 ;;
  esac
}

safe_to_force_kill() {
  local pid="$1"
  local info
  info="$(ps -p "$pid" -o comm= -o args= 2>/dev/null || true)"
  [ -n "$info" ] || return 1
  case "$info" in
    *opencode*) return 0 ;;
    *) return 1 ;;
  esac
}

port_owned_by_target() {
  local target_pid pids pid
  target_pid="$(tmux_target_pid)"
  [ -n "$target_pid" ] || return 1
  pids="$(port_listener_pids)"
  [ -n "$pids" ] || return 1
  for pid in $pids; do
    if [ "$pid" = "$target_pid" ]; then
      continue
    fi
    if process_is_descendant_of "$pid" "$target_pid"; then
      continue
    fi
    return 1
  done
  return 0
}

wait_health() {
  local deadline=$((SECONDS + 30))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if python3 - "$base_url" "$root" <<'PY' >/dev/null 2>&1
from __future__ import annotations
import http.client
import json
import sys
import urllib.parse

base_url = sys.argv[1].rstrip("/")
root = sys.argv[2]
parsed = urllib.parse.urlparse(base_url)
assert parsed.hostname is not None and parsed.port is not None
conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
try:
    conn.request("GET", "/global/health")
    resp = conn.getresponse()
    body = json.loads(resp.read().decode("utf-8"))
finally:
    conn.close()
if not body.get("healthy"):
    raise SystemExit(1)
query = urllib.parse.urlencode({"directory": root})
payload = json.dumps({"text": ""}).encode("utf-8")
conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=2)
try:
    conn.request("POST", f"/tui/append-prompt?{query}", body=payload, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    if resp.status >= 400:
        raise SystemExit(1)
    _ = resp.read()
finally:
    conn.close()
PY
    then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

post_startup_prompt() {
  python3 - "$base_url" "$root" <<'PY'
from __future__ import annotations
import http.client
import json
import sys
import urllib.parse

base_url = sys.argv[1].rstrip("/")
root = sys.argv[2]
prompt = f"""Follow `{root}/MANAGER.md`. Restarted by `omo_manager_restart.sh`.
Run: `~/.config/getagentsmd`; `~/.config/omo_manager/omo_manager_setup_watchers.sh`; `~/.config/omo_manager/omo_pending_watch.py --once --dry-run`.
Continue current pending/report/email refs. If old session was stuck/context-full, stay in this fresh session."""
parsed = urllib.parse.urlparse(base_url)
assert parsed.hostname is not None and parsed.port is not None
query = urllib.parse.urlencode({"directory": root})
for route, payload in (("/tui/append-prompt", {"text": prompt}), ("/tui/submit-prompt", {})):
    body = json.dumps(payload).encode("utf-8")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    try:
        conn.request("POST", f"{route}?{query}", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from {route}: {text}")
    finally:
        conn.close()
PY
}

mkdir -p -m 700 "$log_dir"
chmod 700 "$state_dir" "$log_dir"
: >"$restart_log"
chmod 600 "$restart_log"
lock_file="$state_dir/manager-restart.lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "another manager restart is already running; lock=$lock_file" >&2
  exit 1
fi

command="cd $(printf '%q' "$workdir") && opencode --hostname $(printf '%q' "$manager_host") --port $(printf '%q' "$manager_port") --model $(printf '%q' "$manager_model") ."
log "manager restart time=$(date '+%Y-%m-%d %H:%M:%S %z')"
log "manager-url=$base_url root=$root workdir=$workdir tmux-target=$tmux_target"
describe_port_listener | tee -a "$restart_log"

if [ "$dry_run" -eq 1 ]; then
  echo "would capture pane to $pane_log"
  echo "would leave existing watchers running until replacement health succeeds"
  echo "would send Ctrl-C to $tmux_target, start: $command"
  echo "would wait for $base_url/global/health and refresh watchers"
  exit 0
fi

if ! tmux has-session -t "${tmux_target%%:*}" 2>/dev/null; then
  session_name="${tmux_target%%:*}"
  log "tmux session missing; creating detached session $session_name in $workdir"
  tmux new-session -d -s "$session_name" -c "$workdir"
fi

if ! tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index}' | grep -Fxq "$tmux_target"; then
  echo "tmux target not found after session check: $tmux_target" >&2
  exit 1
fi

if [ -n "${TMUX_PANE:-}" ]; then
  current_pane_id="$(tmux display-message -p '#{pane_id}' 2>/dev/null || true)"
  target_pane_id="$(tmux display-message -p -t "$tmux_target" '#{pane_id}' 2>/dev/null || true)"
  if [ -n "$current_pane_id" ] && [ "$current_pane_id" = "$target_pane_id" ]; then
    echo "refusing to restart the current pane ($tmux_target); run this helper from another shell or tmux pane" >&2
    exit 1
  fi
fi

tmux capture-pane -p -S -2000 -t "$tmux_target" >"$pane_log" 2>/dev/null || true
chmod 600 "$pane_log" 2>/dev/null || true
log "captured prior pane output to $pane_log"

if port_occupied && ! port_owned_by_target && [ "$force_port" -eq 0 ]; then
  describe_port_listener >&2
  echo "port $manager_port is not owned by tmux target $tmux_target pid=$(tmux_target_pid); refusing to restart the wrong pane. Use the listener's tmux pane or rerun with --force-port after verifying the listener is stale." >&2
  exit 1
fi

if ! pane_restartable; then
  current_cmd="$(tmux display-message -p -t "$tmux_target" '#{pane_current_command}' 2>/dev/null || true)"
  echo "tmux pane $tmux_target is running ${current_cmd:-unknown}, not a known shell or manager process; refusing to send Ctrl-C" >&2
  exit 1
fi

tmux send-keys -t "$tmux_target" C-c
if ! wait_port_free; then
  tmux send-keys -t "$tmux_target" C-c
fi
if ! wait_port_free; then
  pids="$(port_listener_pids)"
  if [ -n "$pids" ] && [ "$force_port" -eq 1 ]; then
    for pid in $pids; do
      if ! safe_to_force_kill "$pid" && [ "${OMO_MANAGER_RESTART_ALLOW_KILL_ANY:-}" != "1" ]; then
        echo "refusing to force-kill non-opencode listener pid=$pid on port $manager_port; set OMO_MANAGER_RESTART_ALLOW_KILL_ANY=1 only if you have verified it is safe" >&2
        exit 1
      fi
    done
    log "forcing stale listener pids on $manager_port: $pids"
    # shellcheck disable=SC2086
    kill $pids >/dev/null 2>&1 || true
    sleep 1
    if ! wait_port_free; then
      # shellcheck disable=SC2086
      kill -9 $pids >/dev/null 2>&1 || true
    fi
  else
    echo "port $manager_port is still occupied by: ${pids:-unknown}; rerun with --force-port if this is the stale manager" >&2
    exit 1
  fi
fi

if ! wait_pane_ready; then
  current_cmd="$(tmux display-message -p -t "$tmux_target" '#{pane_current_command}' 2>/dev/null || true)"
  echo "tmux pane $tmux_target did not return to a known shell after Ctrl-C (current command: ${current_cmd:-unknown}); not typing restart command" >&2
  exit 1
fi

tmux send-keys -t "$tmux_target" "$command" Enter
log "started OpenCode in $tmux_target: $command"

if ! wait_health; then
  echo "manager did not become healthy at $base_url; see $restart_log and $pane_log" >&2
  exit 1
fi
log "manager healthy at $base_url"

# Release the restart lock before spawning long-lived watcher processes. Without
# this, nohup children inherit fd 9 and hold the lock forever, causing later
# legitimate restart attempts and dry-run tests to fail as "already running".
flock -u 9 || true
exec 9>&-

if [ "$refresh_watchers" -eq 1 ]; then
  if ! OMO_MANAGER_LOCAL_ENV=/dev/null OMO_MANAGER_URL="$base_url" OMO_WORK_LOGS_ROOT="$root" OMO_MANAGER_STATE_DIR="$state_dir" \
    "$HOME/.config/omo_manager/omo_manager_setup_watchers.sh" | tee -a "$restart_log"; then
    echo "watcher refresh failed; retry with: OMO_MANAGER_LOCAL_ENV=/dev/null OMO_MANAGER_URL=$base_url OMO_WORK_LOGS_ROOT=$(printf '%q' "$root") OMO_MANAGER_STATE_DIR=$(printf '%q' "$state_dir") ~/.config/omo_manager/omo_manager_setup_watchers.sh" >&2
    exit 1
  fi
fi

if [ "$startup_prompt" -eq 1 ]; then
  post_startup_prompt
  log "submitted manager startup prompt"
fi

cat <<EOF | tee -a "$restart_log"
ok: manager restarted
manager-url: $base_url
tmux-target: $tmux_target
logs: $restart_log
prior-pane-log: $pane_log
EOF
