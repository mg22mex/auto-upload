#!/usr/bin/env bash
set -euo pipefail
export HOME="${HOME:-/home/mg}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"

pkill -f cloudflared 2>/dev/null || true
sleep 1
"$SYSTEMCTL" --user stop cloudflared-vapi-bridge.service 2>/dev/null || true
"$SYSTEMCTL" --user start vapi-bridge.service 2>/dev/null || true
sleep 1

if ! curl -fsS --connect-timeout 3 http://127.0.0.1:8000/health >/dev/null; then
  echo "ERROR: nothing listening on 127.0.0.1:8000" >&2
  exit 1
fi
echo "ORIGIN_OK $(curl -fsS http://127.0.0.1:8000/health)"

: > /tmp/tunnel.log
nohup cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 --url http://127.0.0.1:8000 \
  >/tmp/tunnel.log 2>&1 &
echo "QUICK_PID=$!"
sleep 4
URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' /tmp/tunnel.log | tail -1 || true)"
if [[ -z "$URL" ]]; then
  sleep 4
  URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' /tmp/tunnel.log | tail -1 || true)"
fi
[[ -n "$URL" ]] || { echo URL_NOT_FOUND >&2; tail -30 /tmp/tunnel.log >&2; exit 1; }
echo "$URL"
ss -ltnp 2>/dev/null | grep -E ':8000\b' || true
pgrep -af 'cloudflared tunnel' | head -1 || true
