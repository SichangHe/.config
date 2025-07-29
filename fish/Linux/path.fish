set -gx PATH ~/.local/bin $PATH
set -gx PATH /usr/sbin $PATH

# PNPM
set -gx PNPM_HOME "$HOME/.local/share/pnpm"

# Ruby
set -gx PATH /usr/local/opt/ruby/bin $PATH
set -gx PATH /usr/local/lib/ruby/gems/3.4.0/bin $PATH
