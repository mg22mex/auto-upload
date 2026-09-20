# Named Cloudflare tunnel for Vapi bridge (stable hostname)

Production target: **`https://vapi.autosell.mx`**

Quick tunnels (`*.trycloudflare.com`) change URL on every restart. Prefer a
**token-managed named tunnel** (Zero Trust connector).

## DNS (Neubox)

Tunnel connector can be healthy while the public hostname still **NXDOMAIN**.
`autosell.mx` NS are Neubox — add a **CNAME** (or A/AAAA via Cloudflare proxy) for
`vapi` → `<tunnel-uuid>.cfargotunnel.com` in Zero Trust / DNS, then:

```bash
curl -fsS https://vapi.autosell.mx/health
```

Until DNS propagates, use a quick tunnel (`*.trycloudflare.com`) for Vapi tool URLs.

## Recommended: tunnel token (no `cert.pem`)

1. Cloudflare Zero Trust → **Networks → Tunnels** → create/configure tunnel  
   (public hostname `vapi.autosell.mx` → `http://127.0.0.1:8000`).
2. Copy the **tunnel token**.
3. Install on the host that runs `vapi-bridge`:

### Arch (user systemd)

```bash
cd /Extra/Yandex.Disk/Autosell/Auto-upload
umask 077
printf 'TUNNEL_TOKEN=%s\n' '<paste token>' > cloudflared-vapi-bridge.env
bash scripts/apply_token_tunnel.sh
```

Installs `~/.config/cloudflared-vapi-bridge.env` + user unit
`cloudflared-vapi-bridge.service`.

### Oracle fb-worker (system systemd)

```bash
# on VPS as ubuntu (sudo)
sudo install -d -m 755 /etc/cloudflared
sudo install -m 600 cloudflared-vapi-bridge.env /etc/cloudflared/vapi-bridge.env
sudo cp deploy/cloudflared-vapi-bridge.oracle.service /etc/systemd/system/cloudflared-vapi-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared-vapi-bridge
journalctl -u cloudflared-vapi-bridge -n 20 --no-pager
```

`ExecStart` (token via `EnvironmentFile` — not on `ps` argv):

```text
cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 run
```

Confirm journal shows `protocol=http2` / `Registered tunnel connection` and ingress
`vapi.autosell.mx` → `http://localhost:8000`.

## Temporary quick tunnel (testing)

### Arch helpers

```bash
bash scripts/start_quick_tunnel.sh
# → https://….trycloudflare.com
```

### Oracle one-liner

```bash
nohup cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 \
  --url http://localhost:8000 > /tmp/oracle_quick_tunnel.log 2>&1 &
sleep 4
grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' /tmp/oracle_quick_tunnel.log | tail -n 1
```

Do **not** `pkill cloudflared` blindly — that also kills the named connector.
Kill only `--url` quick processes when cutting over.

## Alternate: cert.pem + config.yml

```bash
cloudflared tunnel login
bash scripts/setup_named_cloudflare_tunnel.sh
```

## Vapi Dashboard

Tool server base URL (permanent, after DNS):

```
https://vapi.autosell.mx
```

Paths: `/vapi/inventory` · `/vapi/financing` · `/vapi/tradein` · `/vapi/lead` · `/vapi/crm-lead`
