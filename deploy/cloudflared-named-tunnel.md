# Named Cloudflare tunnel for Vapi bridge (stable hostname)

Production target: **`https://vapi.autosell.mx`**

Quick tunnels (`*.trycloudflare.com`) change URL on every restart. Prefer a
**token-managed named tunnel** (Zero Trust connector).

## Recommended: tunnel token (no `cert.pem`)

1. Cloudflare Zero Trust → **Networks → Tunnels** → create/configure tunnel  
   (public hostname `vapi.autosell.mx` → `http://127.0.0.1:8000`).
2. Copy the **tunnel token**.
3. On this host (real terminal as `mg`, not agent sandbox):

```bash
cd /Extra/Yandex.Disk/Autosell/Auto-upload
# token file is gitignored
umask 077
printf 'TUNNEL_TOKEN=%s\n' '<paste token>' > cloudflared-vapi-bridge.env
bash scripts/apply_token_tunnel.sh
```

That installs:

- `~/.config/cloudflared-vapi-bridge.env` (mode 600)
- `~/.config/systemd/user/cloudflared-vapi-bridge.service`

Unit `ExecStart` (token via `TUNNEL_TOKEN` in EnvironmentFile — not on `ps` argv):

```text
cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 run
```

Confirm journal shows `protocol=http2` / registered connection (no QUIC timeouts):

```bash
journalctl --user -u cloudflared-vapi-bridge -n 20 --no-pager
curl -fsS https://vapi.autosell.mx/health
```

## Temporary quick tunnel (testing)

Stops the named user unit and prints an ephemeral `*.trycloudflare.com` URL:

```bash
bash scripts/start_quick_tunnel.sh
# → https://….trycloudflare.com

# restore permanent hostname:
pkill -f 'cloudflared tunnel .* --url' || true
systemctl --user start cloudflared-vapi-bridge
```

## Alternate: cert.pem + config.yml

```bash
cloudflared tunnel login
bash scripts/setup_named_cloudflare_tunnel.sh
```

## Vapi Dashboard

Tool server base URL (permanent):

```
https://vapi.autosell.mx
```

Paths: `/vapi/inventory` · `/vapi/financing` · `/vapi/tradein` · `/vapi/lead`
