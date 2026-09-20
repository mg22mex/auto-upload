#!/usr/bin/env bash
set -euo pipefail
export HOME="${HOME:-/home/mg}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/usr/bin:/bin:${HOME}/.local/bin:${PATH:-}"
SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
"$SYSTEMCTL" --user restart vapi-bridge.service
sleep 1
"$SYSTEMCTL" --user is-active vapi-bridge.service
curl -fsS http://127.0.0.1:8000/health
echo
