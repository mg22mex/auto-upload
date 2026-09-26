#!/usr/bin/env bash
# Foreground quick tunnel for systemd — log to QUICK_TUNNEL_LOG, never pkill siblings.
set -euo pipefail
LOG="${QUICK_TUNNEL_LOG:-/tmp/oracle_quick_tunnel.log}"
CF="${CLOUDFLARED_BIN:-/usr/local/bin/cloudflared}"
ORIGIN="${QUICK_TUNNEL_ORIGIN:-http://127.0.0.1:8000}"

: > "$LOG"
exec "$CF" tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 \
  --url "$ORIGIN" >>"$LOG" 2>&1
