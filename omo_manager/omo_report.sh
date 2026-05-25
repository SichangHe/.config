#!/usr/bin/env bash
set -euo pipefail
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
env_root="${OMO_WORK_LOGS_ROOT:-}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-http://127.0.0.1:18790}"
if [ -n "$env_root" ]; then root="$env_root"; fi
task_file=""
status=""
message_file=""
agent="${OMO_AGENT_NAME:-agent}"
usage() { echo "Usage: omo_report.sh --task-file FILE --status STATUS --message-file /tmp/report.md [--agent NAME]"; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root="$2"; shift 2 ;;
    --manager-url) manager_url="$2"; shift 2 ;;
    --task-file) task_file="$2"; shift 2 ;;
    --status) status="$2"; shift 2 ;;
    --message-file) message_file="$2"; shift 2 ;;
    --agent) agent="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$task_file" ] || [ -z "$status" ] || [ -z "$message_file" ]; then usage >&2; exit 2; fi
root_real=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$root")
path_real=$(python3 -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve(strict=False))' "$root_real" "$task_file")
case "$path_real" in "$root_real"/*) ;; *) echo "task file escapes root" >&2; exit 2 ;; esac
if [ ! -f "$message_file" ]; then echo "message file not found" >&2; exit 2; fi
mkdir -p "$(dirname "$path_real")"
stamp=$(date '+%Y-%m-%d %H:%M')
tmux_info="unavailable"
if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
  tmux_info=$(tmux display-message -p 'session=#{session_name} window=#{window_index} pane=#{pane_index} pane_id=#{pane_id} cwd=#{pane_current_path}' 2>/dev/null || printf unavailable)
fi
source_line="(from agent ${agent} via omo_report.sh status=${status})"
lock_path="${path_real}.omo_report.lock"
exec 9>"$lock_path"
flock 9
if [ ! -f "$path_real" ]; then : >"$path_real"; fi
python3 - "$path_real" "$message_file" "$source_line" "$stamp" "$agent" "$status" "$PWD" "${OPENCODE:-}" "${TMUX:-}" "$tmux_info" <<'PY'
from __future__ import annotations
import hashlib
import sys
from pathlib import Path
path = Path(sys.argv[1])
message_path = Path(sys.argv[2])
source_line, stamp, agent, status, pwd, opencode, tmux, tmux_info = sys.argv[3:11]
message_bytes = message_path.read_bytes()
message_hash = hashlib.sha256(message_bytes).hexdigest()
hash_line = f"[message-sha256: {message_hash}]"
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
for idx, line in enumerate(lines):
    if line.strip() != "(pending)":
        continue
    block = lines[idx:]
    for next_idx, block_line in enumerate(block[1:], start=idx + 1):
        if block_line.strip() == "(pending)":
            block = lines[idx:next_idx]
            break
    if source_line in block and hash_line in block:
        raise SystemExit(0)
message = message_bytes.decode("utf-8", errors="replace")
block = [
    "",
    "(pending)",
    source_line,
    f"(report manager {stamp} agent={agent} status={status})",
    f"PWD: {pwd}",
    f"OPENCODE: {opencode}",
    f"TMUX: {tmux}",
    f"tmux-info: {tmux_info}",
    hash_line,
    f"message-file: {message_path}",
    "message:",
]
block.extend(f"> {line}" for line in message.splitlines())
if message.endswith("\n"):
    block.append("> ")
path.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
PY
