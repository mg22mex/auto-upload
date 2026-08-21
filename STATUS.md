# Autosell Auto-upload — Status & Roadmap

Last updated: **2026-08-21**

Companion to [README.md](./README.md) and [docs/PROJECT_GUIDE.md](./docs/PROJECT_GUIDE.md).

---

## Operational snapshot

| Area | State | Notes |
|------|-------|-------|
| FB Marketplace sync | **Live** | `account_1` + `account_2`; Playwright sessions on `fb-worker` |
| Catalog scrape + Odoo inventory | **Live** | GitHub Actions `sync.yml` (2× daily) |
| Listing bump / relist | **Live** | Daily incremental, ≥3d age |
| Voice quote webhook | **Live** | `/webhook/voice-lead`, `/voice/webhook`, `/voice/stream` |
| WhatsApp Evolution | **Live** | `autosell_periferico` + `autosell_san_felipe` |
| WhatsApp qualification bot | **Live** | FSM → `HANDOFF_TO_HUMAN` + Odoo |
| VoIP inbound | **Code live** | `/voice/inbound` — configure `VOICE_DID_*` / forward numbers on VPS |
| Marketplace `wa.me` CTAs | **Live** | Branch phones in `listing_cta.py` / env overrides |
| Odoo CRM attribution | **Live** | Tag `MG Quote Lead` + UTM medium/source by channel |
| Meta Messenger | **Paused** | Code complete; awaiting Fanpage admin |
| Native Odoo WA templates | **Paused** | `ODOO_WA_ACCOUNT_*` unset until Meta Manager |
| FB Page feed posts | **Not started** | Marketplace only today (no Graph `/{page}/feed`) |
| account_3 | **Pending** | Clear old FB listings before enabling |

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

---

## Pending / next steps

### 1. Sales rep round-robin & direct notifications
**Blocked on:** inventory capture completion + confirmed branch rep roster.

**Planned:**
1. Round-robin advisor assignment on `crm.team` (extend existing `round_robin_assign_advisor`).
2. On `HANDOFF_TO_HUMAN` (or inbound call activity), send a **1-on-1 WhatsApp** to the assigned rep via Evolution (not the customer line).
3. Include lead phone, vehicle interest, qualification notes, Odoo lead link.

### 2. Facebook Page Messenger & Page feed
- **Messenger:** Graph verify/parse/quote/reply is implemented (`src/meta_gateway/`). Resume when Fanpage admin grants Page Access Token + webhook subscription (`FB_VERIFY_TOKEN`, `FB_PAGE_ACCESS_TOKEN`).
- **Page timeline posting:** Not in scope of current Marketplace Playwright path. Future expansion would use Graph `/{page-id}/feed` with per-page tokens (separate from Marketplace sessions).

### 3. Other backlog
- Enable `account_3` after clearing old Marketplace inventory.
- Optional: native Odoo WhatsApp Cloud API once Meta Manager credentials exist.
- Catalog `Sucursal` field on autosell.mx (improves Marketplace CTA branch accuracy beyond default Periférico).

---

## Quick verification commands

```bash
# CRM attribution / tag unit tests
python -m unittest tests.test_crm_leads src.odoo_sync.test_client.TestCreateOrUpdateLead -q

# WhatsApp + voice inbound tests
python -m unittest src.whatsapp_worker.test_inbound src.voice_gateway.test_webhook -q

# Marketplace CTA encoding
python -m unittest src.facebook.test_listing_cta -q

# FB sessions (on fb-worker)
python scripts/fb_test_session.py --account account_1
python scripts/fb_test_session.py --account account_2
```
