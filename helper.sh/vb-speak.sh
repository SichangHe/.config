#!/bin/bash
set -euo pipefail

TEXT="$1"
BASE="http://localhost:17493"
PROFILE="84b7a5ce-8146-4f58-8f6b-d976e3e1646d"

notify() {
    osascript -e "display notification \"$1\" with title \"VoiceBox\" subtitle \"$2\" sound name \"Basso\""
}

say_fallback() {
    say "$TEXT"
    exit 0
}

TMP=$(mktemp /tmp/vb-XXXXXX)
trap "rm -f $TMP" EXIT

JSON=$(python3 -c "import json,sys; print(json.dumps({'text': sys.argv[1], 'profile': sys.argv[2], 'speed': 1.5}))" "$TEXT" "$PROFILE")

HTTP_STATUS=$(curl -s -o "$TMP" -w "%{http_code}" --connect-timeout 2 \
    -X POST "$BASE/speak" \
    -H "Content-Type: application/json" \
    -d "$JSON" 2> /dev/null) || {
    notify "VoiceBox server is not running. Open the VoiceBox app." "Server offline"
    say_fallback
}

if [ "$HTTP_STATUS" != "200" ]; then
    ERROR=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('detail','Unknown error'))" "$TMP" 2> /dev/null || echo "HTTP $HTTP_STATUS")
    notify "$ERROR" "Generation failed"
    say_fallback
fi
