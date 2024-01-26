#!/usr/bin/env bash
set -e

mkdir ~/bin
cd ~/bin
curl -L https://github.com/nix-community/nix-user-chroot/releases/download/1.2.2/nix-user-chroot-bin-1.2.2-x86_64-unknown-linux-musl \
    -o nix-user-chroot
chmod +x nix-user-chroot

mkdir -m 0755 ~/.nix
./nix-user-chroot ~/.nix bash -c "curl -L https://nixos.org/nix/install | bash"
echo "nix-env -ibA nixpkgs.fish" | ~/bin/nix-user-chroot ~/.nix bash -l
echo 'alias n="~/bin/nix-user-chroot ~/.nix ~/.nix-profile/bin/fish"' > ~/.profile
