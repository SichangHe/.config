#!/usr/bin/env bash
set -euo pipefail
PATH="$HOME/.config/bin:$PATH"
local_env="${OMO_MANAGER_LOCAL_ENV:-$HOME/.config/omo_manager/local.env}"
if [ -f "$local_env" ]; then
  # shellcheck disable=SC1090
  source "$local_env"
fi
root="${OMO_WORK_LOGS_ROOT:-$HOME/work_logs}"
target=""
file=""
start=""
end=""
submit=1
tmux_target=""
usage() {
  cat <<'EOF'
Usage: omo_dispatch.sh --file FILE --start N --end N [--target NAME] --tmux-target SESSION:WINDOW [--no-submit]

Dispatch prompts through the visible tmux target with safe buffer paste.
EOF
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --root) root="$2"; shift 2 ;;
    --file) file="$2"; shift 2 ;;
    --start) start="$2"; shift 2 ;;
    --end) end="$2"; shift 2 ;;
    --target) target="$2"; shift 2 ;;
    --no-submit) submit=0; shift ;;
    --tmux-target) tmux_target="$2"; shift 2 ;;
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
  report_instruction="For manager reports, write the report text to REPORT_FILE through an editor/file-editing tool or other non-shell text channel before calling helpers, then run \`omo_report.sh --task-file ${report_task} --status STATUS --agent agent-name --message-file REPORT_FILE\`; STATUS=blocked|in-progress|done. Email human only if manager unreachable/explicit, using \`omo_email_human.sh --subject-file SUBJECT_FILE --message-file BODY_FILE\`. Verification: aggregate only—command names + pass/fail/failures, no test counts or verbose passing logs. Prefer \`omo_quiet_checks.sh -- \"COMMAND\" [-- \"COMMAND\" ...]\`; if the same repeated command set is used, create/run a tiny-output \`*_quiet_check.*\` wrapper and report only that aggregate output."
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
if [ "$dispatch_done" -eq 0 ] && [ -n "$tmux_target" ]; then
  tmux_instr_file=$(mktemp /tmp/omo-dispatch-tmux-instruction.XXXXXX)
  chmod 600 "$tmux_instr_file"
  printf 'Read the dispatch prompt from %s and follow it exactly.\n' "$prompt_file" >"$tmux_instr_file"
  tmux_send_args=("omo_tmux_send.py" --target "$tmux_target" --message-file "$tmux_instr_file")
  keep_prompt_file=1
  if [ "$submit" -eq 1 ]; then
    tmux_send_args+=(--enter --enter-count "${OMO_DISPATCH_TMUX_ENTER_COUNT:-2}" --ready-timeout-s "${OMO_DISPATCH_TMUX_READY_TIMEOUT_S:-300}")
  fi
  "${tmux_send_args[@]}"
  dispatch_done=1
fi
if [ "$dispatch_done" -eq 0 ]; then
  echo "no tmux target supplied" >&2
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
