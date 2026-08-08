# Fast repost / relist — findings & production config

## Comparison results (Jul 2026)

| Flow | Result | URL | Notes |
|------|--------|-----|--------|
| **Renovar** | Works at 7+ days (FB often gates earlier) | **Same** | Confirmed on Mazda CX-5 (`obj1137`) |
| **Full repost (our Playwright)** | Works | **New** | Mark sold → create; preferred for momentum |
| **FB delete & relist menu** | Not available | — | Dashboard count **0**; dialog empty |
| **Chrome extension** | Works | **New** | Corvette `obj1136` account_2: `1773…` → `2579…` |

## Production choice (relist-first)

- **Wed + Sun cron** (`.github/workflows/repost.yml` `0 15 * * 0,3`) → `scripts/run_weekly_bump.py`
  - **Default (`auto`)** → full **repost/relist** (`run_repost.py`) for live rows with `posted_at` age ≥ **3 days**. Cap **5**/account; Chromium restart every **3**; auto-reopen on `TargetClosedError`.
  - **Optional** `--mode renew` or config `even_week`/`odd_week` → native **Renovar**.
- **Manual override** → Actions → Run workflow → mode `renew` / `repost` / `auto`.
- **Holds** → `fb_repost_hold.py` skips both renew and full repost.
- **Age defaults** → `REPOST_MIN_AGE_DAYS=3`, `RENEW_MIN_AGE_DAYS=3`, `config.yaml` `sync.repost.min_age_days: 3`.

## UI labels (es-MX / EN)

| Action | Spanish | English |
|--------|---------|---------|
| Renew | Renovar publicación / Renovar | Renew listing / Renew |
| More menu | Más | More |
| Selling list | Tus publicaciones | Your listings |

## Commands

```bash
# Default plan (repost ≥3d)
python scripts/run_weekly_bump.py --all-eligible --dry-run

# Force renew or repost
python scripts/run_weekly_bump.py --mode renew --all-eligible --dry-run
python scripts/run_weekly_bump.py --mode repost --account account_2 --ids obj1126

# Direct scripts
python scripts/run_repost.py --all-eligible --older-than 3 --dry-run
python scripts/run_renew.py --account account_2 --ids obj969

# After extension repost
python scripts/fb_set_listing_url.py --account account_2 --autosell-id obj1136 \
  --url 'https://www.facebook.com/marketplace/item/NEWID/'
```
