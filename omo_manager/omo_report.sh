#!/usr/bin/env bash
set -euo pipefail
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
manager_url="${OMO_MANAGER_URL:-http://127.0.0.1:18790}"
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
if [ ! -f "$path_real" ]; then : >"$path_real"; fi
stamp=$(date '+%Y-%m-%d %H:%M')
tmux_info="unavailable"
if [ -n "${TMUX:-}" ] && command -v tmux >/dev/null 2>&1; then
  tmux_info=$(tmux display-message -p 'session=#{session_name} window=#{window_index} pane=#{pane_index} pane_id=#{pane_id} cwd=#{pane_current_path}' 2>/dev/null || printf unavailable)
fi
start_line=$(( $(wc -l <"$path_real") + 2 ))
{
  printf '\n(report manager %s agent=%s status=%s)\n' "$stamp" "$agent" "$status"
  printf 'PWD: %s\n' "$PWD"
  printf 'OPENCODE: %s\n' "${OPENCODE:-}"
  printf 'TMUX: %s\n' "${TMUX:-}"
  printf 'tmux-info: %s\n' "$tmux_info"
  printf 'message-file: %s\n' "$message_file"
} >>"$path_real"
~/.config/omo_manager/omo_push_to_manager.py "report: file=${task_file} line=${start_line} agent=${agent} status=${status}" --manager-url "$manager_url" --root "$root" --submit
