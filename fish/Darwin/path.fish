set -gx PATH /opt/homebrew/bin $PATH
set -gx NODE_PATH /opt/homebrew/lib/node_modules $NODE_PATH

# postgres
set -gx PATH /opt/homebrew/opt/libpq/bin $PATH

# libtorch
set -gx LIBTORCH ~/.local/share/libtorch_v2.1.0/torch
set -gx LD_LIBRARY_PATH "$LIBTORCH/lib" $LD_LIBRARY_PATH
set -gx DYLD_LIBRARY_PATH "$LIBTORCH/lib" $DYLD_LIBRARY_PATH
