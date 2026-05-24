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
prompt="$body"
prompt_file=$(mktemp /tmp/omo-dispatch-prompt.XXXXXX)
chmod 600 "$prompt_file"
printf '%s' "$prompt" >"$prompt_file"
cleanup() { rm -f "$prompt_file" "${tmux_tmp:-}"; }
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
  tmux_tmp=$(mktemp /tmp/omo-dispatch.XXXXXX)
  chmod 600 "$tmux_tmp"
  cp "$prompt_file" "$tmux_tmp"
  tmux load-buffer "$tmux_tmp"
  tmux paste-buffer -t "$tmux_target"
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
