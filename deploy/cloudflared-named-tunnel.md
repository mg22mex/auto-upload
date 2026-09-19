# Named Cloudflare tunnel for Vapi bridge (stable hostname)

Production target: **`https://vapi.autosell.mx`**

Quick tunnels (`*.trycloudflare.com`) change URL on every restart. Use a
**named** tunnel on the Cloudflare zone that owns `autosell.mx`.

## One-time setup (interactive)

`cloudflared tunnel login` **must** run in a real desktop/browser session on this
host (or copy `cert.pem` into `~/.cloudflared/` after authorizing the
`autosell.mx` zone). Agent/CI environments cannot complete the Access callback.

```bash
# 1) Login (browser) — writes ~/.cloudflared/cert.pem
cloudflared tunnel login

# 2–4) create tunnel + DNS + config.yml + restart user unit:
bash scripts/setup_named_cloudflare_tunnel.sh
```

Manual equivalent:

```bash
cloudflared tunnel create vapi-bridge
cloudflared tunnel route dns vapi-bridge vapi.autosell.mx
# then write ~/.cloudflared/config.yml (below) and restart the unit
```

## `~/.cloudflared/config.yml`

```yaml
tunnel: vapi-bridge
credentials-file: /home/mg/.cloudflared/<TUNNEL_UUID>.json
protocol: http2
ingress:
  - hostname: vapi.autosell.mx
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Replace `<TUNNEL_UUID>` with the id from `cloudflared tunnel list`.

## User systemd unit

`~/.config/systemd/user/cloudflared-vapi-bridge.service` (repo:
`deploy/cloudflared-vapi-bridge.user.service`):

```ini
ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 run vapi-bridge
```

```bash
systemctl --user daemon-reload
systemctl --user restart cloudflared-vapi-bridge
journalctl --user -u cloudflared-vapi-bridge -n 30 --no-pager
```

Confirm log shows `protocol=http2` and no QUIC timeouts.

## Vapi Dashboard

Tool server base URL (permanent):

```
https://vapi.autosell.mx
```

Paths: `/vapi/inventory` · `/vapi/financing` · `/vapi/tradein` · `/vapi/lead`
