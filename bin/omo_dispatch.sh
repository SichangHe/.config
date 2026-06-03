#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")/../omo_manager" && pwd) || exit 1
exec sh "$root/omo_dispatch.sh" "$@"
