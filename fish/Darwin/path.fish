set -gx NODE_PATH /opt/homebrew/lib/node_modules $NODE_PATH

# postgres
set -gx PATH /opt/homebrew/opt/libpq/bin $PATH

# PNPM
set -gx PNPM_HOME "$HOME/Library/pnpm"

# Sioyek
set -gx PATH /Applications/sioyek.app/Contents/MacOS/ $PATH

# Antigravity
set -gx PATH "$HOME/.antigravity/antigravity/bin" $PATH
