#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  echo "Usage: natural-syntax-ls.sh [natural-syntax-ls args...]"
  exit 0
fi
if ! command -v natural-syntax-ls >/dev/null; then
  echo "natural-syntax-ls not found" >&2
  exit 127
fi
export LIBTORCH=~/.local/share/libtorch_v2.1.0/torch
export LD_LIBRARY_PATH="$LIBTORCH/lib:${LD_LIBRARY_PATH:-}"
export DYLD_LIBRARY_PATH="$LIBTORCH/lib:${DYLD_LIBRARY_PATH:-}"

natural-syntax-ls "$@"
