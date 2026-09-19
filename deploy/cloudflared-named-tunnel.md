# Named Cloudflare tunnel for Vapi bridge (stable hostname)

Quick tunnels (`*.trycloudflare.com`) change URL on every restart and flap on
Wi‑Fi when using QUIC. Prefer a **named** tunnel once you have a Cloudflare
zone (e.g. `autosell.mx`).

## One-time setup

```bash
cloudflared tunnel login
# Browser auth → writes ~/.cloudflared/cert.pem

cloudflared tunnel create vapi-bridge
# Writes ~/.cloudflared/<TUNNEL_UUID>.json

cloudflared tunnel route dns vapi-bridge vapi.autosell.mx
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: vapi-bridge
credentials-file: /home/mg/.cloudflared/<TUNNEL_UUID>.json
protocol: http2
# Prefer IPv4 when the LAN's IPv6 path to Cloudflare edge is broken.
# edge-ip-version is a CLI flag; for named tunnels also pass:
#   cloudflared tunnel --protocol http2 --edge-ip-version 4 run vapi-bridge
ingress:
  - hostname: vapi.autosell.mx
    service: http://127.0.0.1:8000
  - service: http_status:404
```

## User systemd unit

Replace `ExecStart` in `~/.config/systemd/user/cloudflared-vapi-bridge.service`:

```ini
ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version 4 run vapi-bridge
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user restart cloudflared-vapi-bridge
```

Vapi tool server base URL becomes: `https://vapi.autosell.mx`
