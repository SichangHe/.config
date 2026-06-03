#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")/../omo_manager" && pwd) || exit 1
exec python3 "$root/omo_push_to_manager.py" "$@"
