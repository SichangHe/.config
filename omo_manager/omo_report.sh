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
alloc_message_file=0
agent="${OMO_AGENT_NAME:-agent}"
usage() {
  printf '%s\n' \
    "Usage: omo_report.sh --task-file FILE --status STATUS --message-file FILE [--agent NAME]" \
    "       omo_report.sh --task-file FILE --alloc-message-file" \
    "Create report text in a private helper-allocated file, then pass that path with --message-file. A file named REPORT is refused unless it is in a private owner-only directory."
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root="$2"; shift 2 ;;
    --manager-url) manager_url="$2"; shift 2 ;;
    --task-file) task_file="$2"; shift 2 ;;
    --status) status="$2"; shift 2 ;;
    --message-file) message_file="$2"; shift 2 ;;
    --alloc-message-file) alloc_message_file=1; shift ;;
    --agent) agent="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$task_file" ]; then usage >&2; exit 2; fi
if [ "$alloc_message_file" -eq 1 ] && [ -n "$message_file" ]; then echo "--alloc-message-file cannot be combined with --message-file" >&2; exit 2; fi
if [ "$alloc_message_file" -eq 0 ] && { [ -z "$status" ] || [ -z "$message_file" ]; }; then usage >&2; exit 2; fi
root_real=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$root")
path_real=$(python3 -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve(strict=False))' "$root_real" "$task_file")
case "$path_real" in "$root_real"/*) ;; *) echo "task file escapes root" >&2; exit 2 ;; esac
if [ "$alloc_message_file" -eq 1 ]; then
  python3 - "$path_real" <<'PY'
from __future__ import annotations
import os
import re
import sys
import tempfile
from pathlib import Path
task_path = Path(sys.argv[1])
safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", task_path.stem).strip("._-")[:80] or "task"
drafts_dir = Path("/tmp") / f"omo-report-drafts-{os.getuid()}"
drafts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
drafts_dir.chmod(0o700)
fd, tmp_name = tempfile.mkstemp(prefix=f"{safe_stem}.", suffix=".md", dir=drafts_dir)
os.close(fd)
path = Path(tmp_name)
path.chmod(0o600)
print(path)
PY
  exit 0
fi
python3 - "$message_file" <<'PY'
from __future__ import annotations
import os
import stat
import sys
from pathlib import Path
path = Path(sys.argv[1]).resolve(strict=False)
if path.name != "REPORT":
    raise SystemExit(0)
try:
    parent_stat = path.parent.stat()
except FileNotFoundError:
    parent_stat = None
if parent_stat is not None and parent_stat.st_uid == os.getuid() and stat.S_IMODE(parent_stat.st_mode) & 0o077 == 0:
    raise SystemExit(0)
print(f"refusing shared report path: {sys.argv[1]}", file=sys.stderr)
print("files named REPORT are accepted only from a private owner-only directory", file=sys.stderr)
print("allocate a private path first: omo_report.sh --task-file TASK.md --alloc-message-file", file=sys.stderr)
raise SystemExit(2)
PY
if [ ! -f "$message_file" ]; then echo "message file not found" >&2; exit 2; fi
mkdir -p "$(dirname "$path_real")"
stamp=$(date '+%H:%M')
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
old_legacy_source_line="(from agent ${agent} via omo_report.sh status=${status})"
old_source_prefix="[omo-message-source: origin=agent agent=${agent} via=omo_report.sh status=${status}"
old_source_line="${old_source_prefix}"
tmux_target="${TMUX_PANE:-}"
pointer_label="$agent"
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
  old_source_line="${old_source_line}$(append_kv tmux_session "$tmux_session")"
  old_source_line="${old_source_line}$(append_kv tmux_window_index "$tmux_window_index")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_index "$tmux_pane_index")"
  old_source_line="${old_source_line}$(append_kv tmux_pane_id "$tmux_pane_id")"
  if [ -n "$tmux_session" ] && [ -n "$tmux_window_index" ] && [ -n "$tmux_pane_index" ]; then
    old_source_line="${old_source_line}$(append_kv tmux_target "${tmux_session}:${tmux_window_index}.${tmux_pane_index}")"
  fi
  if [ -n "$tmux_session" ] && [ -n "$tmux_window_index" ]; then
    pointer_label="${tmux_session}:${tmux_window_index}"
    if [ -n "$tmux_pane_index" ] && [ "$tmux_pane_index" != "0" ]; then
      pointer_label="${pointer_label}.${tmux_pane_index}"
    fi
  fi
  old_source_line="${old_source_line}$(append_kv tmux_window_name "$tmux_window_name")"
elif [ -n "$tmux_target" ]; then
  old_source_line="${old_source_line}$(append_kv tmux_pane_id "$tmux_target")"
  pointer_label="$tmux_target"
fi
old_source_line="${old_source_line}]"
old_legacy_source_match="$old_legacy_source_line"
if [ "$old_source_line" != "${old_source_prefix}]" ]; then
  old_legacy_source_match=""
fi
pointer_info=$(python3 - "$message_file" "$agent" "$status" "$stamp" "$path_real" "$pointer_label" <<'PY'
from __future__ import annotations
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
message_path = Path(sys.argv[1])
agent, status, stamp = sys.argv[2:5]
task_path = Path(sys.argv[5])
pointer_label = sys.argv[6]
message_bytes = message_path.read_bytes()
message_hash = hashlib.sha256(message_bytes).hexdigest()

def safe_part(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"

def safe_label(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9:._%-]+", "_", value.strip()).strip("._-")
    return part[:80] or "unknown"

label = safe_label(pointer_label)
task_basename = safe_label(task_path.name)
reports_dir = Path("/tmp") / f"omo-agent-messages-{os.getuid()}"
reports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
reports_dir.chmod(0o700)
report_key = hashlib.sha256(b"\0".join([message_bytes, agent.encode(), status.encode(), label.encode(), str(task_path).encode()])).hexdigest()
report_path = reports_dir / f"{safe_part(agent)}_{safe_part(status)}_{report_key}.md"
pointer_line = f"(from agent {label} {report_path})"
sent_line = f"(sent from {agent} via omo_report.sh tmux={label} time={stamp} task-file={task_basename})"
header = "\n".join(
    [
        sent_line,
        f"[message-sha256: {message_hash}]",
        "message:",
    ]
).encode("utf-8") + b"\n"
body = header + message_bytes
if report_path.exists():
    current = report_path.read_bytes()
    try:
        current_header, current_message = current.split(b"message:\n", 1)
        current_header_text = current_header.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"stale or corrupt report file: {report_path}") from exc
    stable_expected_lines = [
        f"[message-sha256: {message_hash}]",
    ]
    current_header_lines = current_header_text.splitlines()
    if current_message != message_bytes or any(line not in current_header_lines for line in stable_expected_lines):
        raise RuntimeError(f"stale or corrupt report file: {report_path}")
    if current != body:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{report_path.name}.", suffix=".tmp", dir=reports_dir)
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        tmp_path.chmod(0o600)
        os.replace(tmp_path, report_path)
    report_path.chmod(0o600)
else:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{report_path.name}.", suffix=".tmp", dir=reports_dir)
    tmp_path = Path(tmp_name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
    tmp_path.chmod(0o600)
    os.replace(tmp_path, report_path)
    report_path.chmod(0o600)
print(f"{message_hash}\t{report_path}\t{pointer_line}")
PY
)
IFS=$'\t' read -r message_hash durable_message_file pointer_line <<EOF
$pointer_info
EOF
lock_path="${path_real}.omo_report.lock"
exec 9>"$lock_path"
flock 9
if [ ! -f "$path_real" ]; then : >"$path_real"; fi
python3 - "$path_real" "$message_hash" "$pointer_line" "$old_legacy_source_match" "$old_source_line" <<'PY'
from __future__ import annotations
import re
import sys
from pathlib import Path
path = Path(sys.argv[1])
message_hash, pointer_line, old_legacy_source_match, old_source_line = sys.argv[2:6]
hash_line = f"[message-sha256: {message_hash}]"
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

def without_old_report_file(line: str) -> str:
    line = re.sub(r" report_file=[^ \]]+", "", line)
    line = re.sub(r" report_sha256=[^ \]]+", "", line)
    line = re.sub(r" report-file=[^ )]+", "", line)
    return line

def without_volatile_tmux(line: str) -> str:
    line = re.sub(r" tmux_pane_id=[^ \]]+", "", line)
    line = re.sub(r" tmux_window_name=[^ \]]+", "", line)
    return line

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
    stripped_block = [block_line.strip() for block_line in block]
    normalized_block = [without_old_report_file(block_line) for block_line in stripped_block]
    stable_source_block = [without_volatile_tmux(block_line) for block_line in normalized_block]
    stable_old_source_line = without_volatile_tmux(old_source_line)
    if pointer_line in stripped_block:
        raise SystemExit(0)
    if hash_line in stripped_block and (
        stable_old_source_line in stable_source_block
        or (old_legacy_source_match and old_legacy_source_match in normalized_block)
    ):
        raise SystemExit(0)
block = [
    "",
    "(pending)",
    pointer_line,
]
path.write_text("\n".join(lines + block) + "\n", encoding="utf-8")
PY
