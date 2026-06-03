#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")/../omo_manager" && pwd) || exit 1
exec sh "$root/omo_manager_setup_watchers.sh" "$@"
