alias c=clear
alias e=exit
alias rsy='rsync --recursive --inplace --times --compress --exclude "**.DS_STORE" -hP'

# VS Code
if type -q code
    alias co=code
end

# Python3
if type -q ipython
    alias py=ipython
else
    alias py=python3
end

# Neovim
if type -q nvim
    alias vi=nvim
end

# Git
alias gt='git status --short --branch'
alias ga='git add . && gt'
alias gc='git commit -a'
alias gs='if [ (git rev-parse --abbrev-ref HEAD) = main ]
    git remote | xargs -P0 -L1 git push
else
    git push
end && git pull'

# eza
if type -q eza
    alias l='eza --icons'
    alias la='eza -a --icons'
    alias ll='eza -lh --git --icons'
    alias lla='eza -lah --git --icons'
    alias lt='eza -T --icons'
    alias llt='eza -lTh --git --icons'
    alias lat='eza -ahT --icons'
    alias llat='eza -lahT --git --icons'
    alias ld='eza -D --icons'
    alias lad='eza -aD --icons'
    alias lld='eza -lhD --git --icons'
    alias llad='eza -lahD --git --icons'
end

# Kitty
if [ $TERM = "xterm-kitty" ]
    alias ssh='kitten ssh'
end
