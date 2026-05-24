#!/usr/bin/env bash
set -euo pipefail
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
base_url=""
directory="$root"
target=""
file=""
start=""
end=""
submit=1
tmux_fallback=0
usage() {
  cat <<'EOF'
Usage: omo_dispatch.sh --file FILE --start N --end N [--target NAME] [--base-url URL] [--directory DIR] [--no-submit] [--tmux-fallback SESSION:WINDOW.PANE]
EOF
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root="$2"; shift 2 ;;
    --file) file="$2"; shift 2 ;;
    --start) start="$2"; shift 2 ;;
    --end) end="$2"; shift 2 ;;
    --target) target="$2"; shift 2 ;;
    --base-url) base_url="${2%/}"; shift 2 ;;
    --directory) directory="$2"; shift 2 ;;
    --no-submit) submit=0; shift ;;
    --tmux-fallback) tmux_target="$2"; tmux_fallback=1; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$file" ] || [ -z "$start" ] || [ -z "$end" ]; then usage >&2; exit 2; fi
case "$start$end" in (*[!0-9]*) echo "start/end must be positive integers" >&2; exit 2 ;; esac
if [ "$start" -lt 1 ] || [ "$end" -lt "$start" ]; then echo "invalid line range" >&2; exit 2; fi
root_real=$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$root")
path_real=$(python3 -c 'from pathlib import Path; import sys; print((Path(sys.argv[1]) / sys.argv[2]).resolve(strict=False))' "$root_real" "$file")
case "$path_real" in "$root_real"/*) ;; *) echo "file escapes root" >&2; exit 2 ;; esac
if [ ! -f "$path_real" ]; then echo "file not found: $file" >&2; exit 2; fi
body=$(sed -n "${start},${end}p" "$path_real")
if [ -z "$body" ]; then echo "empty dispatch block" >&2; exit 2; fi
manager_line_pattern='^\s*(?:>\s*)*(?:(?:[-*+]\s+(?:\[[ xX]\]\s+)?)|(?:[0-9]+[.)]\s+))*\(for manager:'
manager_lines=$(printf '%s\n' "$body" | python3 -c 'import re, sys; pat = re.compile(sys.argv[1], re.I); sys.stdout.write("\n".join(line for line in sys.stdin.read().splitlines() if pat.match(line)))' "$manager_line_pattern")
prompt=$(printf '%s\n' "$body" | python3 -c 'import re, sys; pat = re.compile(sys.argv[1], re.I); sys.stdout.write("\n".join(line for line in sys.stdin.read().splitlines() if not pat.match(line)))' "$manager_line_pattern")
if [ -z "$prompt" ]; then echo "empty dispatch prompt after removing manager-only lines" >&2; exit 2; fi
report_request=$(printf '%s\n' "$manager_lines" | python3 -c 'import re, sys
lines = sys.stdin.read().splitlines()
manager_prefix = re.compile(sys.argv[1], re.I)
negated_instruction = re.compile(r"\b(?:do\s+not|don.t|no|without)\b[^.;,\n]{0,60}\b(?:ask|tell|instruct|have|ensure|make\s+sure|direct)?\b[^.;,\n]{0,40}\b(?:reports?|omo_report(?:\.sh)?)\b", re.I)
direct = re.compile(r"\b(?:omo_report(?:\.sh)?|report\s+(?:back\s+)?to\s+(?:you|the\s+manager|manager))\b", re.I)
ask = re.compile(r"\b(?:tell|ask|instruct|have|ensure|make\s+sure|direct)\b.{0,80}\b(?:agent|them|it|they)\b.{0,80}\breports?\b.{0,80}\b(?:back|to\s+(?:you|the\s+manager|manager)|questions|blockers|status|completion)\b", re.I)
found = False
for line in lines:
    content = manager_prefix.sub("", line, count=1).rstrip(") ")
    if not negated_instruction.search(content) and (direct.search(content) or ask.search(content)):
        found = True
        break
sys.stdout.write("1" if found else "")' "$manager_line_pattern")
if [ -n "$report_request" ]; then
  report_task=$(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$file")
  report_instruction="Report questions, blockers, status, and completion to the manager via \`~/.config/omo_manager/omo_report.sh --task-file ${report_task} --status STATUS --message-file MESSAGE_FILE --agent agent-name\`, replacing STATUS with blocked, in-progress, or done and MESSAGE_FILE with a unique temp file such as \`\$(mktemp /tmp/omo-report.XXXXXX)\`. Put details in the message file; email the human only if manager is unreachable or explicitly necessary."
  prompt=$(printf '%s\n\n%s' "$prompt" "$report_instruction")
fi
prompt_file=$(mktemp /tmp/omo-dispatch-prompt.XXXXXX)
chmod 600 "$prompt_file"
printf '%s' "$prompt" >"$prompt_file"
keep_prompt_file=0
cleanup() {
  if [ "$keep_prompt_file" -eq 0 ]; then
    rm -f "$prompt_file"
  fi
  rm -f "${tmux_instr_file:-}"
}
trap cleanup EXIT
dispatch_done=0
if [ -n "$base_url" ]; then
  if python3 - "$base_url" "$directory" "$submit" "$prompt_file" <<'PY'
from __future__ import annotations
import json
import sys
import urllib.parse
import urllib.request
base_url, directory, submit, prompt_file = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
prompt = open(prompt_file, encoding="utf-8").read()
query = urllib.parse.urlencode({"directory": directory})
with urllib.request.urlopen(f"{base_url}/global/health", timeout=5) as resp:
    if resp.status >= 400:
        raise RuntimeError(f"target OpenCode TUI lacks /global/health: HTTP {resp.status}")
def post(route: str, payload: object) -> None:
    req = urllib.request.Request(f"{base_url}{route}?{query}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
post("/tui/append-prompt", {"text": prompt})
if submit == "1":
    post("/tui/submit-prompt", {})
PY
  then
    dispatch_done=1
  elif [ "$tmux_fallback" -eq 0 ]; then
    exit 1
  fi
fi
if [ "$dispatch_done" -eq 0 ] && [ "$tmux_fallback" -eq 1 ]; then
  tmux_instr_file=$(mktemp /tmp/omo-dispatch-tmux-instruction.XXXXXX)
  chmod 600 "$tmux_instr_file"
  printf 'Read the dispatch prompt from %s and follow it exactly.\n' "$prompt_file" >"$tmux_instr_file"
  tmux load-buffer "$tmux_instr_file"
  tmux paste-buffer -t "$tmux_target"
  keep_prompt_file=1
  if [ "$submit" -eq 1 ]; then
    tmux send-keys -t "$tmux_target" Enter
  fi
  tmux delete-buffer >/dev/null 2>&1 || true
  dispatch_done=1
fi
if [ "$dispatch_done" -eq 0 ]; then
  echo "no target OpenCode --base-url supplied; tmux fallback is explicit only" >&2
  exit 2
fi
stamp=$(date '+%Y-%m-%d %H:%M:%S')
note="(manager dispatch: ${stamp})"
python3 - "$path_real" "$start" "$end" "$note" <<'PY'
from __future__ import annotations
import sys
from pathlib import Path
path = Path(sys.argv[1])
start = int(sys.argv[2])
end = int(sys.argv[3])
note = sys.argv[4]
lines = path.read_text(encoding="utf-8").splitlines()
delete_from = max(0, start - 2)
delete_to = min(end, len(lines))
delete_indices = [idx for idx in range(delete_from, delete_to) if lines[idx] == "(pending)"]
for idx in reversed(delete_indices):
    del lines[idx]
removed_before_end = sum(1 for idx in delete_indices if idx < end)
insert_at = min(end - removed_before_end, len(lines))
if note not in lines:
    lines.insert(insert_at, note)
    changed = True
else:
    changed = bool(delete_indices)
if changed:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
