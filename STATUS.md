# Autosell Auto-upload — Status & Roadmap

Last updated: **2026-09-27**

Companion to [README.md](./README.md) and [docs/PROJECT_GUIDE.md](./docs/PROJECT_GUIDE.md).

---

## Operational snapshot

| Area | State | Notes |
|------|-------|-------|
| FB Marketplace sync | **Live** | `account_1` + `account_2`; Playwright on `fb-worker`; create URL `/marketplace/create/vehicle` |
| Slot allocator | **Live** | `max_listings_per_account: 40`; `enforce_overflow_removals: true`; **FIFO waitlist rotation** (≤15 yields/account/run) |
| Catalog scrape + Odoo inventory | **Live** | GitHub Actions `sync.yml` (2× daily) + Oracle `odoo-inventory-sync.timer` (every 2h: `sale_ok=False` for web-missing SKUs) |
| Listing bump / relist | **Live** | Daily incremental, ≥2d age |
| Voice quote webhook | **Live** | `/webhook/voice-lead`, `/voice/webhook`, `/voice/stream` |
| **Beatriz Vapi bridge** | **Live on Oracle** | `:8000`; published-stock only; audit logs; timeout **5s**; quick tunnel + **auto Vapi tool URL sync** (`scripts/sync_vapi_tool_urls.py` / `cloudflared-quick-tunnel.service`); TTS lots `*`/`+`; CRM lot→`team_id` RR |
| WhatsApp Evolution | **Live on Oracle** | Docker → `127.0.0.1:8082`; instances `autosell_periferico` + `autosell_san_felipe` (no `autosell_main`). Disable bots: `scripts/disable_evolution_autoreply.py` on the VPS. |
| Cloudflare tunnel | **Connector live** | Named token on VPS; **Neubox DNS for `vapi.autosell.mx` still pending** — use quick `*.trycloudflare.com` until CNAME |
| WhatsApp qualification bot | **Live** | FSM → `HANDOFF_TO_HUMAN` + Odoo |
| Sales rep Round Robin + notify | **Live** | `REPS_*` + `data/rr_cursor.db`; Evolution 1-on-1 cards with identity + `https://wa.me/` |
| VoIP inbound | **Code live** | `/voice/inbound` — configure `VOICE_DID_*` / forward numbers on VPS |
| Marketplace `wa.me` CTAs | **Live** | Branch phones in `listing_cta.py` / env overrides |
| Odoo CRM attribution | **Live** | Tag `MG Quote Lead` + UTM medium/source by channel |
| CrediAuto year term caps | **Live** | `term_limits.py`: ≥Y−2→60m, Y−3/Y−4→48m, ≤Y−5→36m; Beatriz note + PDF Plazo |
| Session `interested_vehicle` | **Live** | Inventory/financing overwrite `wa_qualification.vehicle_interest` + Vapi chat meta; cita binds last vehicle; trade-in → `trade_in_label` only |
| Webform email ingest | **Code live** | IMAP `scripts/parse_web_leads.py` + `web-leads-imap.timer` (2 min); needs `WEB_LEADS_IMAP_*` on Oracle |
| Meta Messenger | **Paused** | Code complete; awaiting Fanpage admin |
| Native Odoo WA templates | **Paused** | `ODOO_WA_ACCOUNT_*` unset until Meta Manager |
| FB Page feed posts | **Not started** | Marketplace only today (no Graph `/{page}/feed`) |
| account_3 | **Pending** | Clear old FB listings before enabling |
| Commission attribution | **Scaffold** | `src/attribution.py` + `data/commissions.db` + Streamlit `dashboard/app.py` (Comisiones tab); reconcile via `scripts/reconcile_commissions.py` |

---

## Completed — Phase 2 (Communication & CRM)

### WhatsApp
- [x] Multi-instance Evolution API (`deploy/docker-compose.evolution.yml`)
- [x] Branch routing: Periférico ↔ San Felipe (`WHATSAPP_INSTANCE_*`, `ODOO_TEAM_*`)
- [x] Inbound webhook `POST /webhook/whatsapp`
- [x] Stateful lead qualification (payment method → trade-in / down payment → handoff)
- [x] Soft-capture CRM upsert + branch auto-reply via Evolution

### Voice / VoIP
- [x] Quote pipeline webhook (STT / structured JSON)
- [x] `POST /voice/inbound` — caller/DID parse, branch team, CRM log, TwiML/JSON dial

### Facebook Marketplace copy
- [x] Dynamic branch WhatsApp CTAs in `vehicle_description()` (`src/facebook/listing_cta.py`)
  - Periférico → `526142274381`
  - San Felipe → `526141293763`

### Odoo CRM & attribution
- [x] Lead tag renamed to **`MG Quote Lead`** (search/create `crm.tag`)
- [x] UTM attribution (`utm.medium` / `utm.source` search-or-create):

  | Channel | Medium | Source |
  |---------| | ------ | ------ |
  | WhatsApp | WhatsApp | Facebook Marketplace |
  | Voice / Inbound Call | Phone | Inbound Call |
  | Web form | Website | Autosell Web |

- [x] Branch sales teams via `ODOO_TEAM_PERIFERICO` / `ODOO_TEAM_SAN_FELIPE` (+ fleet location override)
- [x] Sales rep Round Robin (`RoundRobinAssigner` + `data/rr_cursor.db`) and Evolution rep cards (`notify_appointment_rep`: real identity + `https://wa.me/`)

---

## Pending Integration Backlog

Operational blockers and next product modules. Same section in [docs/PROJECT_GUIDE.md](./docs/PROJECT_GUIDE.md#pending-integration-backlog).

### Environment Secrets
- [x] **`WEB_LEADS_IMAP_*`** on Oracle for `marketing@autosell.mx` (timer `web-leads-imap.timer`).
- [x] Enable timer: `systemctl enable --now web-leads-imap.timer` (via `scripts/deploy_oracle_webhook.sh --imap-check`).

### DNS Migration
- [ ] Neubox **CNAME** `vapi.autosell.mx` → Cloudflare tunnel hostname (named connector already on VPS; retire `*.trycloudflare.com` once live). **Skipped for now** — keep current production tunnel.

### Meta Manager
- [ ] Facebook **Page Access Token** + webhook verify (`FB_VERIFY_TOKEN`, `FB_PAGE_ACCESS_TOKEN`) for `meta_gateway`.
- [x] Messenger interim auto-reply → WhatsApp (`src/meta_gateway/messenger_autoreply.py`, `FB_MESSENGER_WA_LINK`).
- [ ] Meta **WhatsApp Cloud API** / Manager credentials → set `ODOO_WA_ACCOUNT_PERIFERICO` / `ODOO_WA_ACCOUNT_SAN_FELIPE` (native Odoo templates currently `queued_pending_meta` only).

### Lead Conversion & Commission Attribution Module
- [x] **Scaffold (2026-09-27):** `src/attribution.py` ledger (`data/commissions.db`), won/lost stage hook in `OdooTriggerManager`, Odoo reconcile (`scripts/reconcile_commissions.py`), Streamlit tab **Comisiones y Atribución** (`dashboard/app.py`).
- [ ] Tune `COMMISSION_WON_STAGES` to live Odoo stage names; map phone-only reps to durable keys for payroll.
- [ ] Production cron for monthly reconcile + operator review of rates (`COMMISSION_DEFAULT_PERCENTAGE`).

### Other backlog
- Facebook Page Messenger resume + optional Page **feed** posting (`/{page}/feed`).
- Enable `account_3` after clearing old Marketplace inventory.
- Catalog `Sucursal` field on autosell.mx (Marketplace CTA branch accuracy).
- Monitor FIFO rotation after Sep 2026 unlock (waitlist was stuck at 25/25 Free=0).

---

## Quick verification commands

```bash
# Slot allocator / FIFO unit tests
python -m unittest src.sync.test_allocator -q

# CRM attribution / tag unit tests
python -m unittest tests.test_crm_leads src.odoo_sync.test_client.TestCreateOrUpdateLead -q

# WhatsApp + voice inbound tests
python -m unittest src.whatsapp_worker.test_inbound src.voice_gateway.test_webhook -q

# Marketplace CTA encoding
python -m unittest src.facebook.test_listing_cta -q

# Plan-only sync (no FB)
python run_sync.py --dry-run --from-snapshot data/catalog_latest.json

# FB sessions (on fb-worker)
python scripts/fb_test_session.py --account account_1
python scripts/fb_test_session.py --account account_2
```
