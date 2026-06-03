#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: vb-speak.sh TEXT"
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    "") usage >&2; exit 2 ;;
esac

text="$1"
base="${VOICEBOX_BASE:-http://localhost:17493}"
profile="${VOICEBOX_PROFILE:-84b7a5ce-8146-4f58-8f6b-d976e3e1646d}"

notify() {
    if command -v osascript >/dev/null; then
        osascript -e "display notification \"$1\" with title \"VoiceBox\" subtitle \"$2\" sound name \"Basso\"" || true
    fi
}

say_fallback() {
    command -v say >/dev/null && say "$text" || printf '%s\n' "$text"
    exit 0
}

tmp=$(mktemp /tmp/vb-XXXXXX)
trap 'rm -f "$tmp"' EXIT

json=$(python3 -c "import json,sys; print(json.dumps({'text': sys.argv[1], 'profile': sys.argv[2], 'speed': 1.5}))" "$text" "$profile")

http_status=$(curl -s -o "$tmp" -w "%{http_code}" --connect-timeout 2 \
    -X POST "$base/speak" \
    -H "Content-Type: application/json" \
    -d "$json" 2> /dev/null) || {
    notify "VoiceBox server is not running. Open the VoiceBox app." "Server offline"
    say_fallback
}

if [ "$http_status" != "200" ]; then
    error=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('detail','Unknown error'))" "$tmp" 2> /dev/null || echo "HTTP $http_status")
    notify "$error" "Generation failed"
    say_fallback
fi
