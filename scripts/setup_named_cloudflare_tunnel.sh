#!/usr/bin/env bash
# Complete named Cloudflare tunnel setup for vapi.autosell.mx
# Prerequisites: cloudflared in PATH; run `cloudflared tunnel login` first.
set -euo pipefail

HOME_DIR="${HOME:-/home/mg}"
CF_DIR="${HOME_DIR}/.cloudflared"
TUNNEL_NAME="${TUNNEL_NAME:-vapi-bridge}"
HOSTNAME="${VAPI_TUNNEL_HOSTNAME:-vapi.autosell.mx}"
ORIGIN="${VAPI_TUNNEL_ORIGIN:-http://127.0.0.1:8000}"
UNIT_SRC="$(cd "$(dirname "$0")/.." && pwd)/deploy/cloudflared-vapi-bridge.user.service"
UNIT_DST="${HOME_DIR}/.config/systemd/user/cloudflared-vapi-bridge.service"

if [[ ! -f "${CF_DIR}/cert.pem" ]]; then
  echo "Missing ${CF_DIR}/cert.pem — run: cloudflared tunnel login" >&2
  echo "Open the printed dash.cloudflare.com URL and authorize the autosell.mx zone." >&2
  exit 1
fi

mkdir -p "${CF_DIR}" "${HOME_DIR}/.config/systemd/user"

if ! cloudflared tunnel list 2>/dev/null | awk 'NR>1 {print $2}' | grep -qx "${TUNNEL_NAME}"; then
  echo "Creating tunnel ${TUNNEL_NAME}..."
  cloudflared tunnel create "${TUNNEL_NAME}"
else
  echo "Tunnel ${TUNNEL_NAME} already exists."
fi

TUNNEL_ID="$(cloudflared tunnel list | awk -v n="${TUNNEL_NAME}" '$2==n {print $1; exit}')"
if [[ -z "${TUNNEL_ID}" ]]; then
  echo "Could not resolve tunnel id for ${TUNNEL_NAME}" >&2
  exit 1
fi
CREDS="${CF_DIR}/${TUNNEL_ID}.json"
if [[ ! -f "${CREDS}" ]]; then
  echo "Missing credentials file ${CREDS}" >&2
  exit 1
fi

echo "Routing DNS ${HOSTNAME} → tunnel ${TUNNEL_ID}..."
cloudflared tunnel route dns "${TUNNEL_NAME}" "${HOSTNAME}" || true

cat > "${CF_DIR}/config.yml" <<EOF
tunnel: ${TUNNEL_NAME}
credentials-file: ${CREDS}
protocol: http2
ingress:
  - hostname: ${HOSTNAME}
    service: ${ORIGIN}
  - service: http_status:404
EOF

echo "Wrote ${CF_DIR}/config.yml"
cp -f "${UNIT_SRC}" "${UNIT_DST}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user daemon-reload
systemctl --user restart cloudflared-vapi-bridge
sleep 2
systemctl --user is-active cloudflared-vapi-bridge
journalctl --user -u cloudflared-vapi-bridge -n 25 --no-pager | grep -iE 'Registered|protocol=|ERR |hostname|http2' || true
echo
echo "Vapi permanent base URL: https://${HOSTNAME}"
echo "Inventory tool: https://${HOSTNAME}/vapi/inventory"
