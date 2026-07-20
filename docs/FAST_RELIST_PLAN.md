# Fast renew / relist — findings & implementation

## Comparison results (Jul 2026)

| Flow | Result | URL | Notes |
|------|--------|-----|--------|
| **Renovar** | Works at 7+ days | **Same** | Confirmed on Mazda CX-5 (`obj1137`) |
| **FB delete & relist** | Not available | — | Dashboard count **0**; dialog empty |
| **Chrome extension** | Works | **New** | Corvette `obj1136` account_2: `1773…` → `2579…` |

## Production choice

- **Weekly job** → native **Renovar** (`scripts/run_renew.py`) — same URL, ads-safe, seconds per listing, cap **25**/account.
- **New URL when needed** → Chrome extension or `scripts/run_repost.py` (full create), then `fb_set_listing_url.py`.
- **Holds** → `fb_repost_hold.py` skips both renew and full repost.

## UI labels (es-MX / EN)

| Action | Spanish | English |
|--------|---------|---------|
| Renew | Renovar publicación / Renovar (7 días) | Renew listing / Renew (7 days) |
| More menu | Más | More |
| Selling list | Tus publicaciones | Your listings |

## Commands

```bash
# Plan weekly-style renew
python scripts/run_renew.py --all-eligible --dry-run

# Renew one listing
python scripts/run_renew.py --account account_2 --ids obj969

# After extension repost
python scripts/fb_set_listing_url.py --account account_2 --autosell-id obj1136 \
  --url 'https://www.facebook.com/marketplace/item/NEWID/'
```
