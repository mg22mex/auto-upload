#!/usr/bin/env bash
# Benchmark POST /vapi/inventory (local) — prints ms + HTTP body snippet.
set -euo pipefail
export PATH="/usr/bin:/bin:${HOME:-/home/mg}/.local/bin:${PATH:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PAYLOAD='{"message":{"call":{"customer":{"number":"6143231198"}},"toolCalls":[{"id":"call_test","function":{"arguments":{"brand":"Toyota","model":"Corolla"}}}]}}'

python3 - <<'PY'
import json, time, urllib.request
payload = {
  "message": {
    "call": {"customer": {"number": "6143231198"}},
    "toolCalls": [{
      "id": "call_test",
      "function": {
        "arguments": {"brand": "Toyota", "model": "Corolla"}
      }
    }]
  }
}
body = json.dumps(payload).encode()
req = urllib.request.Request(
  "http://127.0.0.1:8000/vapi/inventory",
  data=body,
  headers={"Content-Type": "application/json"},
  method="POST",
)
t0 = time.perf_counter()
with urllib.request.urlopen(req, timeout=30) as resp:
  raw = resp.read()
  code = resp.status
ms = (time.perf_counter() - t0) * 1000
print(f"HTTP={code} ms={ms:.0f}")
print(raw[:600].decode())
PY

# Direct Odoo path timing (bypass HTTP)
.venv/bin/python - <<'PY'
import os, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path('.').resolve() / '.env')
from src.odoo_sync.client import OdooClient
from src.odoo_sync.inventory import query_inventory, build_inventory_domain, cache_clear

cache_clear()
odoo = OdooClient()
odoo.authenticate()

def run(label, **kw):
  t0 = time.perf_counter()
  rows = query_inventory(odoo.execute_kw, **kw)
  ms = (time.perf_counter() - t0) * 1000
  print(f"odoo_{label}_ms={ms:.0f} rows={len(rows)} domain={build_inventory_domain(**{k:v for k,v in kw.items() if k in ('brand','max_price','year','query')})}")
  for r in rows:
    print(' ', r.get('default_code'), r.get('name'), r.get('list_price'))

run('brand_toyota', brand='Toyota')
run('query_toyota_corolla', query='Toyota Corolla')
run('brand_toyota_corolla_as_brand', brand='Toyota Corolla')
# second hit should be cache
t0 = time.perf_counter()
query_inventory(odoo.execute_kw, brand='Toyota')
print(f"odoo_brand_toyota_cached_ms={(time.perf_counter()-t0)*1000:.0f}")
PY
