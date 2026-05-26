#!/usr/bin/env bash
set -euo pipefail
subject=""
message_file=""
usage() { echo "Usage: omo_email_human.sh --subject SUBJECT --message-file FILE"; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --subject)
      if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
      subject="$2"; shift 2 ;;
    --message-file)
      if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
      message_file="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$subject" ] || [ -z "$message_file" ]; then usage >&2; exit 2; fi
subject_lc=$(printf '%s' "$subject" | tr '[:upper:]' '[:lower:]')
case "$subject_lc" in
  "re: [omo_manager]"*) echo "subject must not start with Re: [omo_manager]" >&2; exit 2 ;;
  "[omo]"*|"re: [omo]"*) echo "manager email subject must use [omo_manager]; [omo] is reserved for direct regular-agent email" >&2; exit 2 ;;
  "[omo_manager]"*) ;;
  *) subject="[omo_manager] ${subject}" ;;
esac
if [ ! -f "$message_file" ]; then echo "message file not found" >&2; exit 2; fi
email_helper="$HOME/.config/helper.sh/email_me.py"
if [ ! -x "$email_helper" ]; then echo "email helper not executable: $email_helper" >&2; exit 2; fi
body_file=$(mktemp /tmp/omo-human-email.XXXXXX)
cleanup() { rm -f "$body_file"; }
trap cleanup EXIT
cat "$message_file" >"$body_file"
"$email_helper" "$subject" "$(cat "$body_file")" >/dev/null
state_dir="${OMO_MANAGER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager}"
log_file="$state_dir/human-email-sent.tsv"
{
  mkdir -p "$state_dir" && chmod 700 "$state_dir" 2>/dev/null || true
  python3 - "$log_file" "$subject" <<'PY'
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import sys
path = Path(sys.argv[1])
subject = sys.argv[2].replace("\t", " ").replace("\n", " ")
with path.open("a", encoding="utf-8") as handle:
    handle.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')}\t{subject}\n")
PY
} >/dev/null 2>&1 || true
printf 'Emailed the human\n'
