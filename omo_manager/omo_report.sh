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
usage() {
  printf '%s\n' \
    "Usage: omo_report.sh --task-file FILE --status STATUS --message-file FILE [--agent NAME]" \
    "Create REPORT as a private mktemp/chmod 600 file, then write it through an editor, apply_patch, or another non-shell text channel before calling this helper."
}
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
append_kv() {
  python3 - "$1" "$2" <<'PY'
from __future__ import annotations
import sys
from urllib.parse import quote
key, value = sys.argv[1:3]
if value:
    print(f" {key}={quote(value, safe=':@._/-%')}", end="")
PY
}
report_info=$(python3 - "$root_real" "$message_file" "$agent" "$status" <<'PY'
from __future__ import annotations
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
root = Path(sys.argv[1])
message_path = Path(sys.argv[2])
agent, status = sys.argv[3:5]
message_bytes = message_path.read_bytes()
message_hash = hashlib.sha256(message_bytes).hexdigest()

def safe_part(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"

reports_dir = root / "agent_reports"
reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
if reports_dir.resolve() != Path("/tmp"):
    reports_dir.chmod(0o700)
report_path = reports_dir / f"{safe_part(agent)}_{safe_part(status)}_{message_hash}.txt"
if report_path.exists():
    existing = report_path.read_bytes()
    if existing != message_bytes:
        raise RuntimeError(f"hash collision or stale report file: {report_path}")
    report_path.chmod(0o600)
else:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{report_path.name}.", suffix=".tmp", dir=reports_dir)
    tmp_path = Path(tmp_name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(message_bytes)
    tmp_path.chmod(0o600)
    os.replace(tmp_path, report_path)
    report_path.chmod(0o600)
print(f"{message_hash}\t{report_path}")
PY
)
IFS=$'\t' read -r message_hash durable_message_file <<EOF
$report_info
EOF
legacy_source_line="(from agent ${agent} via omo_report.sh status=${status} report-file=${durable_message_file})"
old_legacy_source_line="(from agent ${agent} via omo_report.sh status=${status})"
source_line="[omo-message-source: origin=agent agent=${agent} via=omo_report.sh status=${status}"
old_source_prefix="${source_line}"
old_source_line="${old_source_prefix}"
source_line="${source_line}$(append_kv report_file "$durable_message_file")"
source_line="${source_line}$(append_kv report_sha256 "$message_hash")"
tmux_target="${TMUX_PANE:-}"
tmux_info=""
if command -v tmux >/dev/null 2>&1; then
  if [ -n "$tmux_target" ]; then
    tmux_info=$(tmux display-message -p -t "$tmux_target" '#{session_name}	#{window_index}	#{pane_index}	#{pane_id}	#{window_name}' 2>/dev/null || true)
  elif [ -n "${TMUX:-}" ]; then
    tmux_info=$(tmux display-message -p '#{session_name}	#{window_index}	#{pane_index}	#{pane_id}	#{window_name}' 2>/dev/null || true)
  fi
fi
if [ -n "$tmux_info" ]; then
  IFS=$'\t' read -r tmux_session tmux_window_index tmux_pane_index tmux_pane_id tmux_window_name <<EOF
$tmux_info
EOF
  source_line="${source_line}$(append_kv tmux_session "$tmux_session")"
  old_source_line="${old_source_line}$(append_kv tmux_session "$tmux_session")"
  source_line="${source_line}$(append_kv tmux_window_index "$tmux_window_index")"
  old_source_line="${old_source_line}$(append_kv tmux_window_index "$tmux_window_index")"
  source_line="${source_line}$(append_kv tmux_pane_index "$tmux_pane_index")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_index "$tmux_pane_index")"
  source_line="${source_line}$(append_kv tmux_pane_id "$tmux_pane_id")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_id "$tmux_pane_id")"
  if [ -n "$tmux_session" ] && [ -n "$tmux_window_index" ] && [ -n "$tmux_pane_index" ]; then
    source_line="${source_line}$(append_kv tmux_target "${tmux_session}:${tmux_window_index}.${tmux_pane_index}")"
    old_source_line="${old_source_line}$(append_kv tmux_target "${tmux_session}:${tmux_window_index}.${tmux_pane_index}")"
  fi
  source_line="${source_line}$(append_kv tmux_window_name "$tmux_window_name")"
  old_source_line="${old_source_line}$(append_kv tmux_window_name "$tmux_window_name")"
elif [ -n "$tmux_target" ]; then
  source_line="${source_line}$(append_kv tmux_pane_id "$tmux_target")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_id "$tmux_target")"
fi
source_line="${source_line}]"
old_source_line="${old_source_line}]"
old_legacy_source_match="$old_legacy_source_line"
if [ "$old_source_line" != "${old_source_prefix}]" ]; then
  old_legacy_source_match=""
fi
lock_path="${path_real}.omo_report.lock"
exec 9>"$lock_path"
flock 9
if [ ! -f "$path_real" ]; then : >"$path_real"; fi
python3 - "$path_real" "$durable_message_file" "$message_hash" "$source_line" "$legacy_source_line" "$old_legacy_source_match" "$old_source_line" "$stamp" "$agent" "$status" <<'PY'
from __future__ import annotations
import sys
from pathlib import Path
path = Path(sys.argv[1])
message_path = Path(sys.argv[2])
message_hash, source_line, legacy_source_line, old_legacy_source_match, old_source_line, stamp, agent, status = sys.argv[3:11]
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
    if any(block_line.strip().startswith(("(manager handled:", "(manager routed:")) for block_line in block[1:]):
        continue
    if hash_line in block and (
        source_line in block
        or (old_legacy_source_match and old_legacy_source_match in block)
        or old_source_line in block
    ):
        raise SystemExit(0)
block = [
    "",
    "(pending)",
    source_line,
    legacy_source_line,
    f"(report manager {stamp} agent={agent} status={status} report-file={message_path})",
    hash_line,
    f"message-file: {message_path}",
]
path.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
PY
