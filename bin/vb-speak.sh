#!/bin/sh
root=$(CDPATH= cd -- "$(dirname -- "$0")/../helper.sh" && pwd) || exit 1
exec sh "$root/vb-speak.sh" "$@"
