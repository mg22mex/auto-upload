#!/usr/bin/env bash
# Deploy latest main to Oracle fb-worker WhatsApp webhook + Vapi bridge.
# Usage:
#   ./scripts/deploy_oracle_webhook.sh
#   ./scripts/deploy_oracle_webhook.sh --clear-phone 5216141754852
set -euo pipefail

KEY="${FB_WORKER_KEY:-/Extra/Yandex.Disk/Autosell/auto-upload-oracle-ssh-key-2026-06-29.key}"
HOST="${FB_WORKER_HOST:-ubuntu@159.54.157.108}"
CLEAR_PHONE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clear-phone) CLEAR_PHONE="${2:-}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

ssh -i "$KEY" -o BatchMode=yes "$HOST" "CLEAR_PHONE=$(printf %q "$CLEAR_PHONE") bash -s" <<'REMOTE'
set -euo pipefail
cd /home/ubuntu/auto-upload
git fetch origin main
git checkout main
git pull --ff-only origin main
echo "HEAD=$(git log -1 --oneline)"
sudo systemctl restart autosell-webhook.service vapi-bridge.service
sleep 2
systemctl is-active autosell-webhook.service vapi-bridge.service
curl -fsS -m 5 http://127.0.0.1:8080/health
echo
curl -fsS -m 5 http://127.0.0.1:8000/health
echo
if [[ -n "${CLEAR_PHONE}" ]]; then
  .venv/bin/python - <<PY
from src.voice_gateway.vapi_chat import clear_wa_session_context
phone = "${CLEAR_PHONE}"
for instance in ("autosell_san_felipe", "autosell_periferico", None):
    print(clear_wa_session_context(phone, instance=instance))
PY
fi
ss -tlnp | grep -E '8000|8080' || true
REMOTE
