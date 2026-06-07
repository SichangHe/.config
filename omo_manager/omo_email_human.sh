#!/usr/bin/env bash
set -euo pipefail
subject=""
message_file=""
usage() { echo "Usage: omo_email_human.sh --subject SUBJECT [--message-file FILE] < BODY"; }
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
if [ -z "$subject" ]; then usage >&2; exit 2; fi
subject_lc=$(printf '%s' "$subject" | tr '[:upper:]' '[:lower:]')
case "$subject_lc" in
  "subject"|"[omo_manager] subject") echo "subject must be a real subject, not the placeholder SUBJECT" >&2; exit 2 ;;
esac
case "$subject_lc" in
  "re: [omo_manager]"*) echo "subject must not start with Re: [omo_manager]" >&2; exit 2 ;;
  "[omo]"*|"re: [omo]"*) echo "manager email subject must use [omo_manager]; [omo] is reserved for direct regular-agent email" >&2; exit 2 ;;
  "[omo_manager]"*) ;;
  *) subject="[omo_manager] ${subject}" ;;
esac
email_helper="$HOME/.config/helper.sh/email_me.py"
if [ ! -x "$email_helper" ]; then echo "email helper not executable: $email_helper" >&2; exit 2; fi
body_file=""
body_file_tmp=0
cleanup_body() {
  if [ "$body_file_tmp" -eq 1 ]; then rm -f "$body_file"; fi
}
trap cleanup_body EXIT
if [ -n "$message_file" ]; then
  if [ ! -f "$message_file" ]; then echo "message file not found" >&2; exit 2; fi
  body_file="$message_file"
else
  body_file=$(mktemp /tmp/omo-human-email.XXXXXX)
  body_file_tmp=1
  chmod 600 "$body_file"
  cat >"$body_file"
fi
python3 - "$body_file" <<'PY'
from pathlib import Path
import sys
if not Path(sys.argv[1]).read_text(encoding="utf-8").strip():
    print("email body must not be empty", file=sys.stderr)
    raise SystemExit(2)
PY
state_dir="${OMO_MANAGER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/omo-manager}"
dedupe_s="${OMO_MANAGER_EMAIL_DEDUPE_S:-300}"
dedupe_result=$(
  { python3 - "$state_dir" "$subject" "$body_file" "$dedupe_s" <<'PY'
from __future__ import annotations
from pathlib import Path
import fcntl
import hashlib
import os
import sys
import time

state_dir = Path(sys.argv[1])
subject = sys.argv[2].replace("\t", " ").replace("\n", " ")
message_file = Path(sys.argv[3])
dedupe_s = int(sys.argv[4])
state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
state_dir.chmod(0o700)
dedupe_file = state_dir / "human-email-dedupe.tsv"
lock_file = state_dir / "human-email-dedupe.lock"
body = message_file.read_bytes()
digest = hashlib.sha256(subject.encode() + b"\0" + body).hexdigest()
now_s = int(time.time())
fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
with os.fdopen(fd, "r+", encoding="utf-8") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    rows: list[tuple[int, str, str]] = []
    try:
        for line in dedupe_file.read_text(encoding="utf-8").splitlines():
            raw_s, old_digest, old_subject = line.split("\t", 2)
            sent_s = int(raw_s)
            if now_s - sent_s <= max(dedupe_s, 0):
                rows.append((sent_s, old_digest, old_subject))
    except OSError:
        pass
    if any(old_digest == digest for _, old_digest, _ in rows):
        print("duplicate")
    else:
        rows.append((now_s, digest, subject))
        tmp = dedupe_file.with_name(f".{dedupe_file.name}.tmp")
        tmp.write_text("".join(f"{sent_s}\t{old_digest}\t{old_subject}\n" for sent_s, old_digest, old_subject in rows), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(dedupe_file)
        print("send")
PY
  } || printf send
)
if [ "$dedupe_result" = "duplicate" ]; then
  printf 'Skipped duplicate human email\n'
  exit 0
fi
"$email_helper" "$subject" <"$body_file" >/dev/null
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
