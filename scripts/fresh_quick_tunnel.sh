#!/usr/bin/env bash
set -euo pipefail
export HOME="${HOME:-/home/mg}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
LOG=/tmp/vapi_quick_tunnel.log

"$SYSTEMCTL" --user stop cloudflared-vapi-bridge.service 2>/dev/null || true
pkill -9 -f cloudflared 2>/dev/null || true
sleep 1

"$SYSTEMCTL" --user start vapi-bridge.service 2>/dev/null || true
sleep 1
curl -fsS --connect-timeout 3 http://127.0.0.1:8000/health >/dev/null

: > "$LOG"
nohup cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 \
  --url http://127.0.0.1:8000 >"$LOG" 2>&1 &
echo "QUICK_PID=$!" >&2
sleep 5
URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG" | tail -n 1 || true)"
if [[ -z "$URL" ]]; then
  sleep 5
  URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG" | tail -n 1 || true)"
fi
if [[ -z "$URL" ]]; then
  echo "URL_NOT_FOUND" >&2
  tail -n 40 "$LOG" >&2
  exit 1
fi

CODE="$(curl -sS -o /tmp/inv_smoke.json -w '%{http_code}' --connect-timeout 45 \
  -X POST "$URL/vapi/inventory" \
  -H 'Content-Type: application/json' \
  -d '{
    "message": {
      "toolCalls": [{
        "id": "call_test_1",
        "function": {
          "name": "get_inventory",
          "arguments": "{\"brand\":\"Mazda\",\"max_price\":\"\",\"year\":null}"
        }
      }]
    }
  }')"
echo "HTTP=$CODE" >&2
head -c 400 /tmp/inv_smoke.json >&2 || true
echo >&2
[[ "$CODE" == "200" ]] || exit 2
echo "$URL"
