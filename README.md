# Auto-upload

Sync [autosell.mx](https://www.autosell.mx) public catalog to **Facebook Marketplace** (Chihuahua, MX), ingest **AI Voice** leads via FastAPI webhook, and keep **Odoo ERP** inventory (`product.template`) + CRM (`crm.lead`) in sync.

**Status:**

- **AI Voice & lead webhook:** Live. FastAPI `POST /webhook/voice-lead` (also `/voice/webhook`, `/voice/stream`) → intent/STT → quote → Odoo lead + 24h follow-up → optional test-drive calendar → PDF attachment → TTS text.
- **Meta Messenger webhook:** `[WIP - Paused awaiting Fanpage Administrator permissions]`. FastAPI `GET`/`POST /webhook/facebook` + quote engine + Odoo lead/chatter + Graph reply are **100% complete in code**; only Meta Developers Page token / webhook subscription remains once Fanpage admin rights are granted.
- **Scrape, diff & FB posting:** Live (`DRY_RUN=false`) for **account_1** and **account_2**. **account_3** excluded until old listings cleared.
- **Listing bump:** Wed + Sun — **full relist/repost** for listings ≥ **3 days** old (`scripts/run_weekly_bump.py`). Native Renovar optional via `--mode renew`.
- **Odoo inventory sync:** Live. Catalog → upsert `product.template` (`default_code = autosell_id`); website-missing SKUs marked **sold** then soft-archived (`active=False`).
- **Modular Odoo sync:** CRM lead manager (dedupe, branch teams, fleet location routing), quote PDF attach, stage/webhook triggers, Fleet VIN, documents. **Native Odoo WhatsApp templates:** code ready; **Meta Cloud API paused** (`ODOO_WA_ACCOUNT_*` unset until Manager credentials).

📖 **[Full project guide](./docs/PROJECT_GUIDE.md)** · **[Setup](./SETUP.md)**

## At a glance

| | |
|--:|--|
| **AI Voice gateway** | FastAPI `POST /webhook/voice-lead` |
| **Meta Messenger** | WIP paused — code done; awaiting Fanpage admin / Page token |
| **Quote engine** | Local French Amortization (Scotiabank profile) |
| **Vehicles** | ~130–134 public catalog from `autosell.mx` |
| **FB accounts** | 3 sessions; **2 live** (`account_1`, `account_2`) |
| **Target FB listings** | ~268 (134 × 2 active accounts) |
| **Odoo** | Modular XML-RPC: CRM, calendar, fleet, PDF quotes, stage triggers, WA (paused), inventory |
| **Schedule** | 2× daily scrape + Odoo sync + FB sync; Wed+Sun relist (≥3d age) |

## System overview

```mermaid
flowchart LR
    SC["Scrape autosell.mx"]
    SNAP["catalog_latest.json"]
    FB["FB Marketplace<br/>sync / renew / repost"]
    DB["sync.db"]
    CALLER["AI Voice / caller"]
    VG["voice_gateway"]
    QE["quote_engine"]
    ODOO["odoo_sync<br/>CRM · Fleet · Quotes · Triggers"]
    LEAD["crm.lead"]
    CAL["calendar.event"]
    WA["WhatsApp templates<br/>(queued pending Meta)"]
    FLEET["fleet.vehicle VIN"]
    PDF["PDF / ir.attachment"]
    MSG["Page Messenger"]
    META["meta_gateway"]
    TRG["triggers<br/>stage / webhook"]

    SC --> SNAP
    SNAP --> FB
    FB --> DB
    SNAP --> ODOO
    CALLER --> VG
    VG --> QE
    QE --> ODOO
    TRG --> ODOO
    ODOO --> LEAD
    ODOO --> CAL
    ODOO --> WA
    ODOO --> FLEET
    ODOO --> PDF
    MSG --> META
    META --> QE
```

**Automated path:** FB reposter (catalog + weekly bump) ➔ Voice / form / stage webhooks ➔ local quote math ➔ `CRMLeadManager` (dedupe + branch/location team) ➔ fleet VIN ➔ `QuotePDFManager` on stage `quoted`/`cotizado` ➔ WhatsApp template **queued** until Meta is live.

Voice / Meta inbound and catalog / FB Marketplace paths share Odoo inventory but keep separate browser sessions. Quote math runs locally before any Odoo / WhatsApp / Messenger payload.

The FB planner only manages listings in **`sync.db`**. It does not scan Facebook’s “Your listings”. Clear old inventory before enabling new accounts (see [PROJECT_GUIDE](./docs/PROJECT_GUIDE.md#go-live-checklist)).

## Pipeline

| Job | Host | Action |
|-----|------|--------|
| **Voice / form webhook** | API host | Quote → `CRMLeadManager` / pipeline → fleet VIN → PDF; stage triggers may queue WA |
| **Odoo stage trigger** | API / worker | Stage `quoted`/`cotizado` → attach quote PDF → **queue** WA (Meta paused) |
| **Meta webhook** | API host | Messenger event → quote → Odoo lead/chatter → Graph API reply |
| **Catalog scrape** | GitHub Actions / `fb-worker` | `autosell.mx` → `catalog_latest.json` |
| **Odoo inventory** | CI step / `fb-worker` | `sync_odoo_inventory.py` → upsert `product.template` |
| **FB sync** | Self-hosted `fb-worker` | Diff → create / update / remove on active accounts |
| **Repost / relist** | Wed+Sun cron / `fb-worker` | Listings ≥3d: mark sold → create new URL (default). Optional `--mode renew` |

## Key scripts

### Voice, quote & Odoo

| Path | Purpose |
|------|---------|
| `src/voice_gateway/webhook.py` | FastAPI app: voice + Meta webhook routes |
| `src/meta_gateway/` | Messenger parse, quote orchestration, Graph API reply |
| `src/pipeline.py` | End-to-end lead: trade-in → quote → Odoo → PDF → WhatsApp |
| `src/quote_engine/` | Local amortization + Scotiabank profile |
| `src/odoo_sync/` | Modular Odoo XML-RPC (see below) |
| `src/whatsapp_worker/client.py` | open-wa / Evolution outbound messages |
| `src/pdf_engine/` | ReportLab quote / vehicle spec PDF (+ optional Odoo `ir.attachment`) |
| `scripts/sync_odoo_inventory.py` | Upsert catalog into `product.template` |
| `scripts/test_live_odoo.py` | Live Odoo smoke (lead + chatter) |
| `scripts/inspect_odoo_inventory.py` | Audit Odoo vehicle products |

### `src/odoo_sync/` modules

| File | Role |
|------|------|
| `base.py` | Shared `OdooClient` session (`authenticate`, `execute_kw`, `ODOO_DRY_RUN`) |
| `client.py` | `OdooCRMClient` — CRM leads, inventory, calendar test-drive, activities (+ mixins) |
| `crm.py` | **`CRMLeadManager`** — dict upsert, phone dedupe + chatter, `ODOO_TEAM_*` branch map, fleet location → team override |
| `quotes.py` | **`QuotePDFManager`** — branch-branded PDF (ReportLab or fallback) + attach to lead chatter |
| `triggers.py` | **`OdooTriggerManager`** / **`process_incoming_webhook`** — stage `quoted`/`cotizado` → PDF + WA queue; inbound forms/voice → CRM |
| `whatsapp.py` | Native Odoo WhatsApp templates. **PAUSED:** Meta Manager pending — leave `ODOO_WA_ACCOUNT_*` unset |
| `fleet.py` | `fleet.vehicle` by VIN/plate → lead; location fields for physical-site routing |
| `documents.py` | `attach_document_to_lead` / `attach_file` via `ir.attachment` |

**Tests:** `tests/test_crm_leads.py`, `tests/test_quotes.py`, `tests/test_triggers.py`, `tests/test_odoo_extensions.py` (all dry-run / mocked XML-RPC friendly).

```bash
# Odoo automation dry-run (no live XML-RPC)
python - <<'PY'
from src.odoo_sync import process_incoming_webhook, OdooTriggerManager
print(process_incoming_webhook({
    "event": "lead_form",
    "client_name": "Ana",
    "phone": "6141234567",
    "vehicle_info": "CX-5",
    "trigger_quote": True,
}, dry_run=True))
PY
python -m unittest tests.test_crm_leads tests.test_quotes tests.test_triggers -q
```

### Facebook Marketplace

| Script | Purpose |
|--------|---------|
| `run_sync.py` | Full sync (scrape + diff + FB; `sync.active_accounts`) |
| `scripts/run_renew.py` | **Renovar** (same URL) |
| `scripts/run_repost.py` | Full repost (sold → create → new URL) |
| `scripts/run_weekly_bump.py` | Listing bump (default **repost/relist**; optional renew) |
| `scripts/fb_repost_hold.py` | Skip renew/repost during FB ads |
| `scripts/fb_login.py` | Headed login per account |
| `scripts/fb_test_session.py` | Verify session |
| `scripts/fb_post_test.py` | Post one vehicle (`--autosell-id obj969`) |

Facebook logic: `src/facebook/` (`poster.py`, `categorize.py`, bilingual EN/ES). **Max 20 photos** per listing.

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env

# Dry-run FB sync
python run_sync.py --dry-run

# Live Odoo smoke + inventory from snapshot
python scripts/test_live_odoo.py
python scripts/sync_odoo_inventory.py --from-snapshot data/snapshots/catalog_latest.json

# Voice + Meta webhooks (dev)
uvicorn src.voice_gateway.webhook:app --reload --port 8080
# Meta: GET/POST /webhook/facebook  (needs FB_VERIFY_TOKEN, FB_PAGE_ACCESS_TOKEN)
```

## Production setup

1. Push to GitHub; add secrets below (also in `.env.example`).
2. Register **`fb-worker`** (Oracle free VPS or Mac Mini).
3. Follow **[SETUP.md](./SETUP.md)** — sessions, form fields, go-live.
4. Review **[docs/PROJECT_GUIDE.md](./docs/PROJECT_GUIDE.md)** — sync rules, Phase 2 architecture.

### Required GitHub Actions secrets (`sync.yml`)

| Secret | Purpose |
|--------|---------|
| `DRY_RUN` | `false` for live FB posts |
| `AUTOSELL_BASE_URL` | Optional; default `https://www.autosell.mx` |
| `ODOO_URL` | e.g. `https://autosellmx.odoo.com` |
| `ODOO_DB` | e.g. `autosellmx` |
| `ODOO_USER` | XML-RPC login. Alias: `ODOO_USERNAME` |
| `ODOO_PASSWORD` | API key/password. Alias: `ODOO_API_KEY` |

Webhook runtime (API host `.env`, not sync.yml): `FB_VERIFY_TOKEN`, `FB_PAGE_ACCESS_TOKEN`, optional `META_DEFAULT_BRANCH_ID` / `VOICE_DEFAULT_BRANCH_ID`.

After each scrape, CI runs `scripts/sync_odoo_inventory.py` on `data/snapshots/catalog_latest.json`.

Active accounts: **`config.yaml`** → `sync.active_accounts` (`account_1`, `account_2`). Override with `--accounts` or `SYNC_ACCOUNTS`.

Persistent state on fb-worker:

- `~/auto-upload-data/data/sync.db`
- `~/auto-upload-data/sessions/account_*`
- Working clone: `~/auto-upload`

## Rollout timeline

```mermaid
gantt
    title Rollout phases
    dateFormat YYYY-MM
    section Done
        Scrape and FB posting             :done, p0, 2026-05, 2026-06
        FB multi-account sessions         :done, p1, 2026-06, 2026-07
        Quote engine Scotiabank calib     :done, p2a, 2026-07, 2026-07
        FastAPI voice webhook             :done, p2b, 2026-07, 2026-07
        Odoo leads and chatter            :done, p2c, 2026-07, 2026-07
        Odoo product.template sync        :done, p2d, 2026-07, 2026-07
        Meta Messenger webhook gateway    :done, p2e, 2026-07, 2026-07
    section Active
        Clear account_3 FB inventory      :active, p3a, 2026-07, 2026-08
        Meta WA Cloud API account IDs     :active, p3wa, 2026-08, 2026-09
        Full automated production         :p3b, 2026-07, 2026-12
    section Done (CRM automation)
        CRM lead mgr + location teams     :done, p2f, 2026-08, 2026-08
        Quote PDF manager + triggers      :done, p2g, 2026-08, 2026-08
```
