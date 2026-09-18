#!/usr/bin/env bash
# Install + enable vapi-bridge (+ optional cloudflared quick tunnel) on this host.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$ROOT/deploy/vapi-bridge.service"
CF_UNIT_SRC="$ROOT/deploy/cloudflared-vapi-bridge.service"
CF_BIN="${CLOUDFLARED_BIN:-/usr/local/bin/cloudflared}"

echo "==> Project: $ROOT"
test -x "$ROOT/.venv/bin/python"
test -f "$ROOT/.env"
test -f "$UNIT_SRC"

echo "==> Install systemd unit"
install -m 644 "$UNIT_SRC" /etc/systemd/system/vapi-bridge.service
systemctl daemon-reload
systemctl enable --now vapi-bridge.service
systemctl --no-pager --full status vapi-bridge.service || true

echo "==> Local health check"
sleep 1
curl -fsS http://127.0.0.1:8000/health
echo

if [[ "${SKIP_CLOUDFLARED:-0}" == "1" ]]; then
  echo "SKIP_CLOUDFLARED=1 — tunnel not installed."
  exit 0
fi

if [[ ! -x "$CF_BIN" ]]; then
  echo "==> Installing cloudflared → $CF_BIN"
  tmp="$(mktemp -d)"
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) asset="cloudflared-linux-amd64" ;;
    aarch64|arm64) asset="cloudflared-linux-arm64" ;;
    *) echo "Unsupported arch: $arch"; exit 1 ;;
  esac
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/${asset}"
  curl -fsSL -o "$tmp/cloudflared" "$url"
  install -m 755 "$tmp/cloudflared" "$CF_BIN"
  rm -rf "$tmp"
  "$CF_BIN" --version
fi

echo "==> Install cloudflared quick-tunnel unit"
install -m 644 "$CF_UNIT_SRC" /etc/systemd/system/cloudflared-vapi-bridge.service
# Ensure ExecStart points at installed binary
sed -i "s|^ExecStart=.*|ExecStart=${CF_BIN} tunnel --no-autoupdate --url http://127.0.0.1:8000|" \
  /etc/systemd/system/cloudflared-vapi-bridge.service
systemctl daemon-reload
systemctl enable --now cloudflared-vapi-bridge.service
sleep 3
systemctl --no-pager --full status cloudflared-vapi-bridge.service || true

echo "==> Public URL (from journal):"
journalctl -u cloudflared-vapi-bridge.service -n 80 --no-pager | \
  grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | tail -1 || \
  echo "(URL not yet in journal — run: journalctl -u cloudflared-vapi-bridge -f)"
