#!/usr/bin/env bash
# Deploy latest main to Oracle fb-worker WhatsApp webhook + Vapi bridge + IMAP timer.
# Usage:
#   ./scripts/deploy_oracle_webhook.sh
#   ./scripts/deploy_oracle_webhook.sh --clear-phone 5216141754852
#   ./scripts/deploy_oracle_webhook.sh --imap-check
set -euo pipefail

KEY="${FB_WORKER_KEY:-/Extra/Yandex.Disk/Autosell/auto-upload-oracle-ssh-key-2026-06-29.key}"
HOST="${FB_WORKER_HOST:-ubuntu@159.54.157.108}"
CLEAR_PHONE=""
IMAP_CHECK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clear-phone) CLEAR_PHONE="${2:-}"; shift 2 ;;
    --imap-check) IMAP_CHECK=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

ssh -i "$KEY" -o BatchMode=yes "$HOST" \
  "CLEAR_PHONE=$(printf %q "$CLEAR_PHONE") IMAP_CHECK=$(printf %q "$IMAP_CHECK") bash -s" <<'REMOTE'
set -euo pipefail
cd /home/ubuntu/auto-upload
git fetch origin main
git checkout main
git pull --ff-only origin main
echo "HEAD=$(git log -1 --oneline)"

# Ensure IMAP unit files are installed (idempotent).
if [[ -f deploy/web-leads-imap.service && -f deploy/web-leads-imap.timer ]]; then
  sudo cp deploy/web-leads-imap.service deploy/web-leads-imap.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now web-leads-imap.timer
fi

sudo systemctl restart autosell-webhook.service vapi-bridge.service
# Kick one IMAP poll now (oneshot service).
sudo systemctl start web-leads-imap.service || true
sleep 2
systemctl is-active autosell-webhook.service vapi-bridge.service
systemctl is-active web-leads-imap.timer || true
systemctl list-timers web-leads-imap.timer --no-pager || true
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
if [[ "${IMAP_CHECK}" == "1" ]]; then
  echo "=== IMAP check ==="
  PYTHONPATH=. .venv/bin/python scripts/parse_web_leads.py --check
fi
ss -tlnp | grep -E '8000|8080' || true
REMOTE
