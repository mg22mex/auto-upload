#!/usr/bin/env bash
# Wait for cloudflared quick-tunnel URL in log, then PATCH Vapi tool server.urls.
# Used as ExecStartPost for deploy/cloudflared-quick-tunnel.oracle.service.
set -euo pipefail

ROOT="${VAPI_BRIDGE_ROOT:-/home/ubuntu/auto-upload}"
LOG="${QUICK_TUNNEL_LOG:-/tmp/oracle_quick_tunnel.log}"
WAIT_SEC="${VAPI_SYNC_WAIT_SEC:-90}"
PY="${ROOT}/.venv/bin/python"
SCRIPT="${ROOT}/scripts/sync_vapi_tool_urls.py"

cd "$ROOT"
export PATH="/usr/bin:/bin:${PATH:-}"

if [[ ! -x "$PY" ]]; then
  echo "ERROR: missing $PY" >&2
  exit 1
fi
if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: missing $SCRIPT" >&2
  exit 1
fi

echo "wait_and_sync: log=$LOG wait=${WAIT_SEC}s" >&2
exec "$PY" "$SCRIPT" --from-log "$LOG" --wait-sec "$WAIT_SEC"
