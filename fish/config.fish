# Nix
if test -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
    source '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
else if test -e "$HOME/.nix-profile/etc/profile.d/nix.fish"
    source "$HOME/.nix-profile/etc/profile.d/nix.fish"
end
set -gx NIXPKGS_ALLOW_UNFREE 1

# proxy
# source ~/.config/fish/proxy.fish

#! kernel specific {
set UNAME (uname)
if [ $UNAME = Darwin ] #! Darwin {{
    # path
    source ~/.config/fish/Darwin/path.fish

    # alias
    source ~/.config/fish/Darwin/alias.sh

    # proxy
    source ~/.config/fish/Darwin/proxy.fish
    #! }}
else if [ $UNAME = Linux ] #! Linux {{
    # path
    source ~/.config/fish/Linux/path.fish
    #! }}
end
#! }

# path
source ~/.config/fish/path.fish

# Bloody Pip global install.
set -gx PIP_BREAK_SYSTEM_PACKAGES 1

# alias
source ~/.config/fish/alias.fish

# starship
if type -q starship
    starship init fish | source
end

# zoxide
if type -q zoxide
    zoxide init fish | source
end

# pay-respects (successor of the Fuck)
if type -q pay-respects
    pay-respects fish --alias | source
end

# default editor
set -gx EDITOR nvim

# white theme for bat and moar
set -gx BAT_THEME GitHub
set -gx MOOR --style=github

# fzf use fd
set -gx FZF_DEFAULT_COMMAND 'fd -H --strip-cwd-prefix -E ".git"'

# sccache
if type -q sccache
    set -gx RUSTC_WRAPPER sccache
end

# Rustc target native cpu.
set -gx RUSTFLAGS "$RUSTFLAGS -C target-cpu=native"

# Cargo use Git CLI.
set -gx CARGO_NET_GIT_FETCH_WITH_CLI true

# Fuck Microsoft .NET telemetry.
set -gx DOTNET_CLI_TELEMETRY_OPTOUT true

# Less no animation
set -gx LESS -XF

if type -q moor
    set -gx PAGER moor
end

# uv
if type -q uv
    uv generate-shell-completion fish | source
end
if type -q uvx
    uvx --generate-shell-completion fish | source
end

if type -q zellij
    zellij setup --generate-completion fish | source
end

# bun
set --export BUN_INSTALL "$HOME/.bun"
set --export PATH $BUN_INSTALL/bin $PATH

# opencode
fish_add_path /Users/sichanghe/.opencode/bin
