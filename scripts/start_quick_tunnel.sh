#!/usr/bin/env bash
# Start (or reuse) a detached quick tunnel. NEVER kills existing cloudflared.
set -euo pipefail
export HOME="${HOME:-/home/mg}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${QUICK_TUNNEL_LOG:-/tmp/vapi_quick_tunnel.log}"

# Reuse an already-running quick tunnel (--url) if present.
if pgrep -f 'cloudflared tunnel .* --url http://127.0.0.1:8000' >/dev/null 2>&1 \
  || pgrep -f 'cloudflared tunnel .* --url http://localhost:8000' >/dev/null 2>&1; then
  for f in "$LOG" /tmp/quicktunnel.log /tmp/tunnel.log; do
    [[ -f "$f" ]] || continue
    URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$f" | tail -1 || true)"
    if [[ -n "$URL" ]]; then
      echo "REUSING_QUICK_TUNNEL $URL" >&2
      echo "$URL"
      exit 0
    fi
  done
  echo "Quick tunnel process alive but URL not in logs — check journal." >&2
  pgrep -fa 'cloudflared tunnel' >&2 || true
  exit 1
fi

# Named tunnel (token / run without --url) may stay up — we do not stop it.
if pgrep -f 'cloudflared tunnel' >/dev/null 2>&1; then
  echo "NOTE: other cloudflared process(es) left running (no pkill)." >&2
  pgrep -fa 'cloudflared tunnel' >&2 || true
fi

: > "$LOG"
nohup cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 \
  --url http://127.0.0.1:8000 >>"$LOG" 2>&1 &
echo "QUICK_PID=$!" >&2
sleep 5
URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG" | tail -1 || true)"
if [[ -z "$URL" ]]; then
  sleep 5
  URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "$LOG" | tail -1 || true)"
fi
if [[ -z "$URL" ]]; then
  echo "URL_NOT_FOUND" >&2
  tail -n 30 "$LOG" >&2 || true
  exit 1
fi
echo "$URL"
# Optional: push new hostname into Vapi tool server.urls (needs VAPI_API_KEY|VAPI_TOKEN).
if [[ "${VAPI_SYNC_ON_QUICK_TUNNEL:-1}" == "1" ]]; then
  PY="${ROOT}/.venv/bin/python"
  SYNC="${ROOT}/scripts/sync_vapi_tool_urls.py"
  if [[ -x "$PY" && -f "$SYNC" ]]; then
    "$PY" "$SYNC" --base-url "$URL" >&2 || \
      echo "WARN: Vapi tool URL sync failed (tunnel still up: $URL)" >&2
  fi
fi
