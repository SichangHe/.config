# Run this with sudo.
export YABAI_CERT=
brew services stop yabai
codesign -fs 'yabai-cert' $(which yabai)
sudo yabai --uninstall-sa
sudo yabai --load-sa
brew services start yabai