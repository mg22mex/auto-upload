#!/usr/bin/env bash
set -euo pipefail
export HOME="${HOME:-/home/mg}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"

"$SYSTEMCTL" --user stop cloudflared-vapi-bridge.service 2>/dev/null || true
pkill -f "${HOME}/.local/bin/cloudflared tunnel" 2>/dev/null || true
sleep 1

: > /tmp/quicktunnel.log
nohup cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 --url http://127.0.0.1:8000 \
  >/tmp/quicktunnel.log 2>&1 &
echo "QUICK_PID=$!"
sleep 5

URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' /tmp/quicktunnel.log | tail -1 || true)"
if [[ -z "$URL" ]]; then
  sleep 5
  URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' /tmp/quicktunnel.log | tail -1 || true)"
fi

tail -n 25 /tmp/quicktunnel.log >&2 || true
if [[ -z "$URL" ]]; then
  echo "URL_NOT_FOUND" >&2
  exit 1
fi
echo "$URL"
