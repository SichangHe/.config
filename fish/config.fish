# Nix
if test -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
    source '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
else if test -e "$HOME/.nix-profile/etc/profile.d/nix.fish"
    source "$HOME/.nix-profile/etc/profile.d/nix.fish"
end

# path
source ~/.config/fish/path.fish

# proxy
# source ~/.config/fish/proxy.fish

#! kernel specific {

if [ (uname) = Darwin ] #! Darwin {{
    # path
    source ~/.config/fish/Darwin/path.fish

    # alias
    source ~/.config/fish/Darwin/alias.sh

    # proxy
    source ~/.config/fish/Darwin/proxy.fish
    #! }}

else if [ (uname) = Linux ] #! Linux {{
    # path
    source ~/.config/fish/Linux/path.fish
    # for nix
    set -ge LD_LIBRARY_PATH
    # Bloody Pip global install.
    set -gx PIP_BREAK_SYSTEM_PACKAGES 1
    #! }}
end
#! }

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

# default editor
set -gx EDITOR nvim

# white theme for bat
set -gx BAT_THEME GitHub

# fzf use fd
set -gx FZF_DEFAULT_COMMAND 'fd -H --strip-cwd-prefix -E ".git"'

# sccache
if type -q sccache
    set -gx RUSTC_WRAPPER sccache
end

# Cargo use Git CLI.
set -gx CARGO_NET_GIT_FETCH_WITH_CLI true

# Bloody Pip global install.
set -gx PIP_BREAK_SYSTEM_PACKAGES 1
