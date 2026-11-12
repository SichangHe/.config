#!/bin/bash
output=$("$HOME/.local/bin/mac-stt" toggle 2>&1)
status=$?

if ((status != 0)); then
    osascript -e 'on run argv
    display notification (item 1 of argv) with title "mac-stt toggle failed"
  end run' "$output"
elif [[ "$output" != "recording" ]]; then
    osascript -e 'tell application "System Events" to keystroke "v" using command down'
fi

exit "$status"
