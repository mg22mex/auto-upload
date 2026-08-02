# Fast renew / relist — findings & implementation

## Comparison results (Jul 2026)

| Flow | Result | URL | Notes |
|------|--------|-----|--------|
| **Renovar** | Works at 7+ days | **Same** | Confirmed on Mazda CX-5 (`obj1137`) |
| **FB delete & relist** | Not available | — | Dashboard count **0**; dialog empty |
| **Chrome extension** | Works | **New** | Corvette `obj1136` account_2: `1773…` → `2579…` |

## Production choice

- **Sunday cron** (`.github/workflows/repost.yml`) → `scripts/run_weekly_bump.py`
  - **Even ISO week** → native **Renovar** (`run_renew.py`) — same URL, ads-safe, seconds/listing, cap **25**/account; Chromium restart every **10**.
  - **Odd ISO week** → full **repost** (`run_repost.py`) — new URL, slower, cap **5**/account; Chromium restart every **3**; auto-reopen on `TargetClosedError`.
- **Manual override** → Actions → Run workflow → mode `renew` / `repost` / `auto`.
- **Holds** → `fb_repost_hold.py` skips both renew and full repost.

## UI labels (es-MX / EN)

| Action | Spanish | English |
|--------|---------|---------|
| Renew | Renovar publicación / Renovar (7 días) | Renew listing / Renew (7 days) |
| More menu | Más | More |
| Selling list | Tus publicaciones | Your listings |

## Commands

```bash
# This Sunday's automatic mode (plan only)
python scripts/run_weekly_bump.py --all-eligible --dry-run

# Force renew or repost regardless of week
python scripts/run_weekly_bump.py --mode renew --all-eligible --dry-run
python scripts/run_weekly_bump.py --mode repost --account account_2 --ids obj1126

# Direct scripts (same as before)
python scripts/run_renew.py --all-eligible --dry-run
python scripts/run_renew.py --account account_2 --ids obj969

# After extension repost
python scripts/fb_set_listing_url.py --account account_2 --autosell-id obj1136 \
  --url 'https://www.facebook.com/marketplace/item/NEWID/'
```
