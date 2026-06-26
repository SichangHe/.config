#!/usr/bin/env bash
set -euo pipefail
subject_file=""
message_file=""
script_path="${BASH_SOURCE[0]}"
if resolved_script=$(readlink -f -- "$script_path" 2>/dev/null); then
  script_path="$resolved_script"
fi
script_dir="$(cd -- "$(dirname -- "$script_path")" && pwd)"
usage() {
  cat <<'EOF'
Usage: omo_email_human.sh --subject-file FILE --message-file FILE

Deprecated compatibility wrapper for email_me.py.
Message body accepts Markdown input; plain text is preferred.

Manager-safe input:
  subject_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-subject.XXXXXX")
  body_file=$(mktemp "${TMPDIR:-/tmp}/omo-email-body.XXXXXX")
  chmod 600 "$subject_file" "$body_file"
  Write both files through an editor, apply_patch, or another non-shell text channel.
  omo_email_human.sh --subject-file "$subject_file" --message-file "$body_file"
EOF
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --subject-file)
      if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
      subject_file="$2"; shift 2 ;;
    --message-file)
      if [ "$#" -lt 2 ]; then usage >&2; exit 2; fi
      message_file="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [ -z "$subject_file" ] || [ -z "$message_file" ]; then usage >&2; exit 2; fi
if [ ! -f "$subject_file" ]; then echo "subject file not found" >&2; exit 2; fi
email_helper="$(cd -- "$script_dir/.." && pwd)/helper.sh/email_me.py"
if [ ! -x "$email_helper" ]; then echo "email helper not executable: $email_helper" >&2; exit 2; fi
if [ ! -f "$message_file" ]; then echo "message file not found" >&2; exit 2; fi
exec "$email_helper" --manager-human --subject-file "$subject_file" --message-file "$message_file"
