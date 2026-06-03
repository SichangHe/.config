set -gx PATH ~/.config/bin $PATH
set -gx PATH /usr/local/bin $PATH
set -gx PATH ~/.local/bin $PATH
set -gx PATH ~/.cargo/bin $PATH
set -gx PATH ~/.pub-cache/bin $PATH
set -gx PATH ~/go/bin $PATH # Go-Lang
set -gx PATH ~/.dotnet/tools/ $PATH # .NET Tools
set -gx PATH "$PNPM_HOME" $PATH # PNPM
# Ruby gems
set -gx GEM_HOME ~/gems
set -gx PATH $GEM_HOME/bin $PATH
# Bun
set -gx BUN_INSTALL "$HOME/.bun"
set -gx PATH "$BUN_INSTALL/bin $PATH"
