#!/usr/bin/env bash
export LIBTORCH=~/.local/share/libtorch_v2.1.0/torch
export LD_LIBRARY_PATH="$LIBTORCH/lib:$LD_LIBRARY_PATH"
export DYLD_LIBRARY_PATH="$LIBTORCH/lib:$DYLD_LIBRARY_PATH"

# We do want to pass each argument to the command.
#shellcheck disable=SC2068
natural-syntax-ls $@
