#!/usr/bin/env bash
# Apply token-based named Cloudflare tunnel on the real host (user systemd).
# Requires: cloudflared-vapi-bridge.env next to this repo (gitignored) OR
#           ~/.config/cloudflared-vapi-bridge.env with TUNNEL_TOKEN=...
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_SRC="${ROOT}/cloudflared-vapi-bridge.env"
ENV_DST="${HOME}/.config/cloudflared-vapi-bridge.env"
UNIT_SRC="${ROOT}/deploy/cloudflared-vapi-bridge.user.service"
UNIT_DST="${HOME}/.config/systemd/user/cloudflared-vapi-bridge.service"

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
JOURNALCTL="$(command -v journalctl || echo /usr/bin/journalctl)"


if [[ ! -f "$ENV_DST" ]]; then
  [[ -f "$ENV_SRC" ]] || {
    echo "Missing tunnel token file." >&2
    echo "Create ${ENV_DST} with: TUNNEL_TOKEN=<token>" >&2
    exit 1
  }
  install -d -m 755 "${HOME}/.config"
  install -m 600 "$ENV_SRC" "$ENV_DST"
else
  echo "Using existing ${ENV_DST}"
fi

grep -q '^TUNNEL_TOKEN=.\+' "$ENV_DST"

install -d -m 755 "${HOME}/.config/systemd/user"
# overwrite even if agent left a root-owned unit behind
rm -f "$UNIT_DST" 2>/dev/null || true
cp -f "$UNIT_SRC" "$UNIT_DST"
chmod 644 "$UNIT_DST"

# Stop any ad-hoc quick tunnel leftovers
pkill -f 'cloudflared tunnel .* --url http://127.0.0.1:8000' 2>/dev/null || true

"$SYSTEMCTL" --user daemon-reload
"$SYSTEMCTL" --user enable cloudflared-vapi-bridge.service
"$SYSTEMCTL" --user restart cloudflared-vapi-bridge.service
sleep 3
"$SYSTEMCTL" --user is-active cloudflared-vapi-bridge.service
echo '--- journal ---'
"$JOURNALCTL" --user -u cloudflared-vapi-bridge -n 15 --no-pager
echo '--- health ---'
curl -fsS --connect-timeout 15 https://vapi.autosell.mx/health
echo
