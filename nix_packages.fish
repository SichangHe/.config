#!/usr/bin/env fish
nix-env -ibA nixpkgs.aria
nix-env -ibA nixpkgs.bat
nix-env -ibA nixpkgs.btop
nix-env -ibA nixpkgs.cargo-cache
nix-env -ibA nixpkgs.cargo-expand
nix-env -ibA nixpkgs.cargo-nextest
nix-env -ibA nixpkgs.cargo-update
nix-env -ibA nixpkgs.cmake
nix-env -ibA nixpkgs.delta
nix-env -ibA nixpkgs.du-dust
nix-env -ibA nixpkgs.eza
nix-env -ibA nixpkgs.fd
nix-env -ibA nixpkgs.fzf
nix-env -ibA nixpkgs.git-lfs
nix-env -ibA nixpkgs.jq
nix-env -ibA nixpkgs.mpv
nix-env -ibA nixpkgs.ncdu
nix-env -ibA nixpkgs.neofetch
nix-env -ibA nixpkgs.neovim
nix-env -ibA nixpkgs.nodejs
nix-env -ibA nixpkgs.onefetch
nix-env -ibA nixpkgs.ripgrep
nix-env -ibA nixpkgs.sccache
nix-env -ibA nixpkgs.sd
nix-env -ibA nixpkgs.starship
nix-env -ibA nixpkgs.tealdeer
nix-env -ibA nixpkgs.thefuck
nix-env -ibA nixpkgs.vale
nix-env -ibA nixpkgs.zoxide

set UNAME (uname)
if [ $UNAME = Darwin ] # Darwin
nix-env -ibA nixpkgs.asciinema
nix-env -ibA nixpkgs.cargo-release
nix-env -ibA nixpkgs.coreutils
nix-env -ibA nixpkgs.exercism
nix-env -ibA nixpkgs.go
nix-env -ibA nixpkgs.gnused
nix-env -ibA nixpkgs.pkg-config
nix-env -ibA nixpkgs.poppler_utils # for pdfimages
nix-env -ibA nixpkgs.protobuf
nix-env -ibA nixpkgs.qpdf
nix-env -ibA nixpkgs.speedtest-cli
nix-env -ibA nixpkgs.tmux
nix-env -ibA nixpkgs.tokei
nix-env -ibA nixpkgs.unpaper
nix-env -ibA nixpkgs.unzip
nix-env -ibA nixpkgs.xdg-ninja

else if [ $UNAME = Linux ] # Linux
nix-env -ibA nixpkgs.nodejs
nix-env -ibA nixpkgs.unzip

end
