#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")/../helper.sh" && pwd) || exit 1
exec python3 "$root/email_me.py" "$@"
