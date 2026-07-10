# Fast relist — pre-implementation plan

Goal: repost listings in **~1–3 minutes each** (extension-like) instead of **~10–15 minutes** (full autosell create with 20 photo uploads).

Current slow path: `mark_sold` → `create_vehicle_listing()` (full composer from autosell.mx).

Target fast path: use Facebook’s **in-place relist / duplicate** flow (same photos & fields already on FB).

---

## What we know

| | [Marketplace Listings Reposter](https://chromewebstore.google.com/detail/marketplace-listings-repo/omkhphhbdgbccmbglgddbonncpghohag) | Our automation today |
|--|--|--|
| Mechanism | Delete/relist via FB UI in your browser | Mark sold + full create from autosell |
| Photos | Kept from existing listing | Re-downloaded & re-uploaded (20) |
| Time per car | ~1–3 min (user reports) | ~10–15 min |
| Volume | User picks any count | Capped (10/account/week) |
| URL sync | Manual | Automatic (`sync.db`) |
| Holds | Remember yourself | `repost_holds` table |

The extension does **not** expose source code. We must **discover the exact FB UI flow** on your accounts (es-MX) before coding.

---

## Decisions to lock before coding

Answer these after discovery (section below):

1. **Which FB flow?** (pick one primary path)
   - **A — Native “Renew”** via `/marketplace/selling/renew_listings/` ← *likely lighter than full relist*
   - **B — Native “Delete & relist”** via `/marketplace/selling/relist_items/` ← *closest to extension*
   - **C — Mark sold + full create** (current slow path) — fallback only
   - **D — Hybrid:** try B (or A if same URL), fallback C

2. **Mark sold vs delete?**  
   Today we use `mark_sold`. Extension may delete. Test which works with the fast path.

3. **Fallback policy**  
   If fast relist fails → auto retry with full create? or skip and log?

4. **Cap after fast path**  
   If relist is 3× faster, is **20–30/account/week** acceptable, or keep 10?

5. **Price/description sync**  
   Fast relist copies **FB** state. If autosell price changed since last post, do we:
   - run `update` before relist,
   - update after relist,
   - or accept stale price until next daily sync?

---

## Pre-flight checklist (do before implementation)

### 1. Run UI discovery script (required)

On fb-worker or local with headed browser:

```bash
cd ~/auto-upload && source .venv/bin/activate
python scripts/fb_explore_relist.py --account account_1 --autosell-id obj969 --headed
```

Outputs under `data/logs/facebook/relist_explore/`:
- Screenshots of listing page, menu, dashboard
- JSON dump of visible buttons / menuitems / links containing repost keywords

Repeat for **account_2** (may differ if EN vs es-MX UI).

### 2. Manual extension comparison (required)

On the **same listing**, time both flows:

| Step | Extension | Notes |
|------|-----------|-------|
| Open dashboard | | |
| Select 1 listing | | |
| Repost | start timer | |
| New listing live | stop timer | |
| New URL | copy | |

Then on VM (dry-run full repost is NOT needed — use a test id):

```bash
# Current slow path (optional baseline — ~10+ min)
time python scripts/run_repost.py --account account_1 --ids objXXXX
```

Record: total time, whether old listing marked sold, new URL, “being reviewed” message.

### 3. Capture extension network/DOM (recommended)

In Brave/Chrome with extension:

1. DevTools → Network, filter `facebook.com`
2. Repost **one** listing
3. Save HAR or note GraphQL operation names / URLs
4. Note which page URLs appear (`/marketplace/...`)

This reveals whether the extension uses a **hidden API** vs pure DOM clicks.

### 4. Document es-MX labels (required)

**Discovery run (account_1, obj969, Jul 2026)** found Facebook’s native seller tools on the dashboard:

| Action | Label (account_1 UI) | URL |
|--------|----------------------|-----|
| **Renew** (bump?) | `To renew` (count) | `/marketplace/selling/renew_listings/?is_routable_dialog=true` |
| **Delete & relist** | `To delete & relist` (count) | `/marketplace/selling/relist_items/?is_routable_dialog=true&show_only_delete_and_relist=true` |

Individual listing page (`/marketplace/item/...`) **did not** show repost keywords in the ⋮ menu from headless scan — the fast path likely lives in these **dashboard dialogs**, not on the item page.

**Still to confirm manually:**
- Spanish labels on account_2 (if es-MX differs from EN “To renew”)
- What happens inside each dialog (checkboxes? bulk confirm?)
- Whether **renew** vs **delete & relist** matches extension behavior
- Time per listing in each flow
- Whether `renew` keeps the same item URL (update) vs new URL (relist)

Fill after headed inspection:

| Action | Spanish label | English label | Locator notes |
|--------|---------------|---------------|---------------|
| Dashboard renew | | To renew | `renew_listings` URL |
| Dashboard delete & relist | | To delete & relist | `relist_items` URL |
| Confirm in dialog | | | |
| Publish after relist | | Publicar | reuse `ui.py` |

### 5. Verify URL capture (required)

Fast relist must still call `store.record_repost()` with a **verified** new item URL (same rules as create — brand + price/model).

Test: after manual/extension repost, run:

```bash
python scripts/fb_find_listing.py --account account_1 --autosell-id obj969
```

Confirm URL matches the **new** item, not the old sold one.

### 6. Edge cases to test manually (before bulk)

| Case | Expected |
|------|----------|
| Listing on **repost hold** | Skipped (already implemented) |
| Listing with **active FB ad** | Do not relist — hold |
| **20-photo** listing | Photos preserved in fast path? |
| **Todoterreno** (Can-Am) | Fields preserved? |
| **Shelby/Ford** listing | |
| Listing **“being reviewed”** | Can it relist? |
| **Session expired** mid-batch | Stop batch (already in executor) |

### 7. Risk / volume decision (business)

After fast path works on 2–3 test listings:

- Propose new defaults: e.g. `max_per_account_per_run: 25`, `min_age_days: 7`
- Full catalog rotation: 134 ÷ 25 ≈ **5–6 weeks** vs 14 weeks today
- Still far less aggressive than reposting all 134 in one session

---

## Proposed implementation phases (after checklist)

### Phase 1 — Discovery module
- `src/facebook/relist.py` — `explore_listing_actions()`, `fast_relist_listing()`
- `scripts/fb_explore_relist.py` (included now)
- `scripts/fb_relist_test.py` — one listing, `--headed`, logs timing

### Phase 2 — Wire into reposter
- `reposter.py`: `REPOST_MODE=fast|full|auto` (default `auto`)
- `auto` = try fast, fallback full create on failure
- Reuse `record_repost()`, holds, weekly workflow unchanged

### Phase 3 — Tune caps
- Raise weekly cap once fast path stable
- Optional: second mid-week run with lower cap

### Phase 4 — Retire extension (optional)
- Same holds + URL sync, faster rotation, no manual checkbox UI

---

## Success criteria

- [ ] Fast relist **≤ 4 minutes** per listing (photos included, no re-upload from autosell)
- [ ] New URL saved in `sync.db` and verifies on item page
- [ ] Old listing marked sold (not duplicate active listings)
- [ ] Works on account_1 and account_2 (es-MX)
- [ ] Fallback to full create works when fast path fails
- [ ] 5 consecutive test relists without checkpoint/session loss

---

## What NOT to do yet

- Do not raise weekly cap until fast path is proven on 5+ listings
- Do not remove full-create repost path (keep as fallback)
- Do not automate account_3 until cleared

---

## Next step

1. Run `fb_explore_relist.py` on 2 accounts  
2. Share the JSON + screenshots (or paste button labels from JSON)  
3. Implement Phase 1–2 based on discovered flow
