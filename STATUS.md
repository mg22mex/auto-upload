# Autosell Auto-upload — Status & Roadmap

Last updated: **2026-09-29**

Companion to [README.md](./README.md), [docs/SYSTEM_DOCUMENTATION.md](./docs/SYSTEM_DOCUMENTATION.md), and [docs/PROJECT_GUIDE.md](./docs/PROJECT_GUIDE.md).

---

## Operational snapshot

| Area | State | Notes |
|------|-------|-------|
| FB Marketplace sync | **Live** | `account_1` + `account_2`; Playwright on `fb-worker`; create URL `/marketplace/create/vehicle` |
| Slot allocator | **Live** | `max_listings_per_account: 40`; `enforce_overflow_removals: true`; **FIFO waitlist rotation** (≤15 yields/account/run) |
| Catalog scrape + FB sync | **Live** | GitHub Actions `sync.yml` **every 3h** UTC (+ 14:00 inventory window) |
| Odoo product inventory | **Live** | Same workflow, **gated 2×/day** (08:00 & 18:00 Chihuahua); **diff-only** `scripts/sync_odoo_inventory.py` (~1s when unchanged) |
| Listing bump / relist | **Live** | Daily incremental, ≥2d age |
| Voice quote webhook | **Live** | `/webhook/voice-lead`, `/voice/webhook`, `/voice/stream` |
| **Beatriz Vapi bridge** | **Live on Oracle** | `:8000`; published-stock only; audit logs; timeout **5s**; quick tunnel + **auto Vapi tool URL sync**; TTS lots `*`/`+`; CRM lot→`team_id` RR |
| WhatsApp Evolution | **Live on Oracle** | Docker → `127.0.0.1:8082`; instances `autosell_periferico` + `autosell_san_felipe` |
| Cloudflare tunnel | **Connector live** | Named token on VPS; **Neubox DNS for `vapi.autosell.mx` deferred** — use quick `*.trycloudflare.com` until CNAME |
| WhatsApp qualification bot | **Live** | Welcome **once**; price/disponibilidad → Odoo stock; cita → `HANDOFF_TO_HUMAN` |
| Sales rep Round Robin + notify | **Live** | `REPS_*` + `data/rr_cursor.db`; Evolution 1-on-1 cards |
| VoIP inbound | **Code live** | `/voice/inbound` — configure `VOICE_DID_*` / forward numbers on VPS |
| Marketplace `wa.me` CTAs | **Live** | Branch phones in `listing_cta.py` / env overrides |
| Odoo CRM attribution | **Live** | Tag `MG Quote Lead` + UTM (WA Directo / FB Messenger / FB Lead Form / Formulario Web / Inbound Call). XML-RPC user **`contabilidad@autosell.mx`** |
| Meta Messenger / Lead Ads | **Live (WA redirect)** | Autoreply → `wa.me/526142274381` + Odoo lead; leadgen webhook registered |
| CrediAuto year term caps | **Live** | `term_limits.py`: ≥Y−2→60m, Y−3/Y−4→48m, ≤Y−5→36m |
| Session `interested_vehicle` | **Live** | Inventory/financing overwrite; cita binds last vehicle |
| Webform email ingest | **Paused** | IMAP code + timer live; **awaiting Gmail App Password** (soft-fail `AUTHENTICATIONFAILED`) |
| Native Odoo WA templates | **Paused** | `ODOO_WA_ACCOUNT_*` unset until Meta Manager |
| Executive Streamlit BI | **Live** | `dashboard/app.py` → [gerencia-comercial-autosell.streamlit.app](https://gerencia-comercial-autosell.streamlit.app); **active production leads only** |
| FB Page feed posts | **Not started** | Marketplace only today (no Graph `/{page}/feed`) |
| account_3 | **Pending** | Clear old FB listings before enabling |
| Commission attribution | **Scaffold** | `src/attribution.py` + `data/commissions.db`; CLI `scripts/reconcile_commissions.py` (BI tabs are Gerencia-focused) |

---

## Completed — Phase 2 (Communication & CRM)

### WhatsApp
- [x] Multi-instance Evolution API (`deploy/docker-compose.evolution.yml`)
- [x] Branch routing: Periférico ↔ San Felipe (`WHATSAPP_INSTANCE_*`, `ODOO_TEAM_*`)
- [x] Inbound webhook `POST /webhook/whatsapp`
- [x] Stateful lead qualification (payment method → trade-in / down payment → handoff)
- [x] Soft-capture CRM upsert + branch auto-reply via Evolution
- [x] Anti-welcome-loop: `format_ai_reply(..., already_greeted=)` greets only on first turn; menu picks / price asks never re-emit `Recibimos tu mensaje…`
- [x] Price / disponibilidad → Odoo published stock (`lookup_inventory_for_interest` / `parse_stock_vehicle_query`) with price + lot

### Voice / VoIP
- [x] Quote pipeline webhook (STT / structured JSON)
- [x] `POST /voice/inbound` — caller/DID parse, branch team, CRM log, TwiML/JSON dial

### Facebook Marketplace copy
- [x] Dynamic branch WhatsApp CTAs in `vehicle_description()` (`src/facebook/listing_cta.py`)
  - Periférico → `526142274381`
  - San Felipe → `526141293763`

### Odoo CRM & attribution
- [x] Lead tag **`MG Quote Lead`** (search/create `crm.tag`)
- [x] UTM attribution (`utm.medium` / `utm.source` search-or-create):

  | Channel | Medium | Source |
  | ---: | ------ | ------ |
  | WhatsApp | WhatsApp | WA Directo |
  | Facebook Messenger | Facebook | FB Messenger |
  | Facebook Lead Ads | Facebook Ads | FB Lead Form |
  | Voice / Inbound Call | Phone | Inbound Call |
  | Web form | Website | Formulario Web |

- [x] Branch sales teams via `ODOO_TEAM_PERIFERICO` / `ODOO_TEAM_SAN_FELIPE` (+ fleet location override)
- [x] Sales rep Round Robin (`RoundRobinAssigner` + `data/rr_cursor.db`) and Evolution rep cards (`notify_appointment_rep`)
- [x] Test-lead hard purge (`scripts/cleanup_odoo_test_data.py --hard-delete`) + dashboard active-only filter
- [x] XML-RPC standardized on **`contabilidad@autosell.mx`** (`.env` / Streamlit secrets)

### Gerencia Comercial (Streamlit)
- [x] Four executive tabs: KPIs + funnel, Control Diario, Financiamientos & Perdidos, Junta Semanal (PDF/Excel)
- [x] Cloud deploy config: `.streamlit/config.toml` (dark), `secrets.toml.example`, `dashboard/requirements-cloud.txt`
- [x] Credentials: `st.secrets` → `ODOO_*` env fallback (`dashboard/secrets_util.py`)

---

## Pending Integration Backlog

Operational blockers and next product modules. Same section in [docs/PROJECT_GUIDE.md](./docs/PROJECT_GUIDE.md#pending-integration-backlog).

### Environment Secrets
- [~] **`WEB_LEADS_IMAP_*`** on Oracle for `marketing@autosell.mx`; timer enabled. **Gmail AUTH failed** — needs Google **App Password** (2FA); then `scripts/parse_web_leads.py --check`.
- [x] Deploy path: `scripts/deploy_oracle_webhook.sh --imap-check`.

### DNS Migration
- [ ] Neubox **CNAME** `vapi.autosell.mx` → Cloudflare tunnel hostname (**deferred** — keep current production tunnel).

### Meta Manager
- [x] Messenger / Lead Ads → WhatsApp redirect + Odoo lead (`meta_gateway`).
- [ ] Optional: expand Graph quote depth / Page **feed** posting.
- [ ] Meta **WhatsApp Cloud API** → set `ODOO_WA_ACCOUNT_*` (native Odoo templates currently soft-fail).

### Lead Conversion & Commission Attribution Module
- [x] **Scaffold:** `src/attribution.py` ledger, stage hook, `scripts/reconcile_commissions.py`.
- [ ] Tune `COMMISSION_WON_STAGES`; monthly reconcile cron + payroll review.

### Other backlog
- Enable `account_3` after clearing old Marketplace inventory.
- Catalog `Sucursal` field on autosell.mx (Marketplace CTA branch accuracy).
- Monitor FIFO rotation after Sep 2026 unlock.

---

## Quick commands

```bash
# Unit tests (CI)
python -m unittest discover -s src -p 'test_*.py' -q
python -m unittest discover -s tests -p 'test_*.py' -q

# Hard-purge smoke/test CRM leads
PYTHONPATH=. python scripts/cleanup_odoo_test_data.py --hard-delete --skip-sales

# Diff-only inventory (seconds when unchanged)
PYTHONPATH=. python scripts/sync_odoo_inventory.py --from-snapshot data/catalog_latest.json

# Local Gerencia dashboard
PYTHONPATH=. streamlit run dashboard/app.py --server.port 8501
```
