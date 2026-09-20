#!/usr/bin/env bash
# Ensure origin + print quick-tunnel URL. NEVER pkill cloudflared.
# To force a new quick URL, stop the old --url process yourself, then re-run
# scripts/start_quick_tunnel.sh
set -euo pipefail
export HOME="${HOME:-/home/mg}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

"$SYSTEMCTL" --user start vapi-bridge.service 2>/dev/null || true
sleep 1
if ! curl -fsS --connect-timeout 3 http://127.0.0.1:8000/health >/dev/null; then
  echo "ERROR: nothing listening on 127.0.0.1:8000" >&2
  exit 1
fi
echo "ORIGIN_OK $(curl -fsS http://127.0.0.1:8000/health)" >&2

URL="$(bash "$ROOT/scripts/start_quick_tunnel.sh")"
echo "$URL"
ss -ltnp 2>/dev/null | grep -E ':8000\b' >&2 || true
pgrep -af 'cloudflared tunnel' >&2 | head -5 || true
