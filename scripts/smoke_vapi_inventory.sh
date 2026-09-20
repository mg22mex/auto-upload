#!/usr/bin/env bash
set -euo pipefail
export PATH="/usr/bin:/bin:${HOME:-/home/mg}/.local/bin:${PATH:-}"
LOG="${QUICK_TUNNEL_LOG:-/tmp/vapi_quick_tunnel.log}"
URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1 || true)"
[[ -n "$URL" ]] || URL="$(bash "$(dirname "$0")/start_quick_tunnel.sh")"
echo "URL=$URL"

post() {
  local label="$1" body="$2"
  local code
  code="$(curl -sS -o /tmp/inv_resp.json -w '%{http_code}' --connect-timeout 45 \
    -X POST "$URL/vapi/inventory" \
    -H 'Content-Type: application/json' \
    -d "$body")"
  echo "$label HTTP=$code"
  head -c 500 /tmp/inv_resp.json; echo
  [[ "$code" == "200" ]]
}

post vapi_envelope '{
  "message": {
    "toolCalls": [{
      "id": "call_test_1",
      "function": {
        "name": "get_inventory",
        "arguments": "{\"brand\":\"Mazda\",\"max_price\":\"\",\"year\":null}"
      }
    }]
  }
}'

post soft_null '{"brand":"","max_price":"null","year":"undefined"}'
echo OK
