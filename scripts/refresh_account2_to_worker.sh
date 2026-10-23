#!/usr/bin/env bash
# Run this in YOUR desktop terminal (Konsole) — needs interactive Facebook login UI.
# Mirrors scripts/refresh_account1_to_worker.sh for account_2 (personal Marco profile).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${FB_WORKER_KEY:-/Extra/Yandex.Disk/Autosell/auto-upload-oracle-ssh-key-2026-06-29.key}"
HOST="${FB_WORKER_HOST:-ubuntu@159.54.157.108}"
SESSION="$ROOT/sessions/account_2"
cd "$ROOT"
source .venv/bin/activate

# Playwright-bundled Chromium often SIGTRAP (signal 5) on Arch/Plasma Wayland.
# Prefer system chromium + X11 ozone + no GPU for headed login.
export FB_BROWSER_CHANNEL="${FB_BROWSER_CHANNEL:-chromium}"
export FB_OZONE_PLATFORM="${FB_OZONE_PLATFORM:-x11}"
export FB_DISABLE_GPU="${FB_DISABLE_GPU:-1}"

# Profile must be writable by you (root:root from sandbox runs causes crashes/locks).
if [[ -d "$SESSION" ]] && [[ ! -w "$SESSION" || ( -d "$SESSION/Default" && ! -w "$SESSION/Default" ) ]]; then
  echo "Session not writable by $USER — fixing ownership (sudo)…"
  sudo chown -R "$USER:$USER" "$SESSION"
fi
# Stale locks after SIGTRAP coredump
rm -f "$SESSION"/SingletonLock "$SESSION"/SingletonCookie "$SESSION"/SingletonSocket 2>/dev/null || true

echo "=== 1) Headed Facebook login for account_2 ==="
echo "Browser: channel=$FB_BROWSER_CHANNEL ozone=$FB_OZONE_PLATFORM disable_gpu=$FB_DISABLE_GPU"
echo "Leave the window OPEN until after you press Enter in this terminal."
python scripts/fb_login.py --account account_2

echo "=== 2) Verify locally ==="
python scripts/fb_test_session.py --account account_2

echo "=== 3) Pack lean session ==="
cd sessions
tar --exclude='*/Cache' --exclude='*/Code Cache' --exclude='*/GPUCache' \
    --exclude='*/Service Worker' --exclude='*/BrowserMetrics' \
    --exclude='*/GPUPersistentCache' --exclude='*/ShaderCache' \
    --exclude='*/GraphiteDawnCache' --exclude='*/GrShaderCache' \
    -czf /tmp/account_2-session.tgz account_2

echo "=== 4) Upload to fb-worker ($HOST) ==="
scp -i "$KEY" /tmp/account_2-session.tgz "$HOST:/tmp/"
ssh -i "$KEY" "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/auto-upload
mkdir -p ~/auto-upload-data/sessions
# replace account_2
rm -rf ~/auto-upload-data/sessions/account_2
mkdir -p ~/auto-upload-data/sessions
tar -xzf /tmp/account_2-session.tgz -C ~/auto-upload-data/sessions
ln -snf ~/auto-upload-data/sessions ~/auto-upload/sessions
ln -snf ~/auto-upload-data/data ~/auto-upload/data
source .venv/bin/activate
python scripts/fb_test_session.py --account account_2
echo "Done on worker."
REMOTE
echo "=== Complete ==="
