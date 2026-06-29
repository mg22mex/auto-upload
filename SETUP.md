# Setup: GitHub + fb-worker (Oracle VPS or Mac Mini)

# Split architecture (updated)

**Important:** autosell.mx does **not** respond to GitHub cloud runner IPs (connect timeout). Scrape and Facebook both run on **fb-worker** — not `ubuntu-latest`.

| Step | Host | Does |
|------|------|------|
| **sync** job | **fb-worker** | Scrape → diff → (Phase 2) Facebook |

Your daily PC can stay off. Only **fb-worker** must be always on and registered before workflows can run.

Phase 0–1: scrape + diff planning. **Phase 2 (Facebook):** Playwright posting is implemented and verified manually on `account_1` (test vehicle `obj969` — 2020 Audi A3). Scheduled sync still uses `DRY_RUN=true` until you enable live posting for all accounts.

---

## Where data lives

| Path | Location | In git? |
|------|----------|---------|
| `data/catalog_latest.json` | Written on fb-worker each run; optional GitHub artifact | No |
| `data/sync.db` | **`~/auto-upload-data/data/` on fb-worker** | No |
| `sessions/account_*` | **`~/auto-upload-data/sessions/` on fb-worker** | No |
| `data/logs/facebook/` | Debug screenshots on fb-worker (`obj969_*.png`, etc.) | No |

The scrape step must run on fb-worker because autosell.mx blocks GitHub-hosted datacenter IPs.

The workflow symlinks `data/` and `sessions/` to `~/auto-upload-data/` so `sync.db` survives each checkout.

---

## Choose your fb-worker

| Option | Cost | Best for |
|--------|------|----------|
| **[Oracle Cloud Always Free](https://www.oracle.com/cloud/free/)** (ARM, 2 OCPU / 12 GB) | $0 | No Mac Mini yet; pick Mexico Central or Monterrey at signup |
| **Mac Mini** (M1/M2, 8–16 GB RAM) | power only | Best Facebook experience (home/residential IP) |
| **Hetzner / DO / Vultr** | ~$5–6/mo | If Oracle capacity fails |

**Not suitable:** Hugging Face Spaces, Streamlit Cloud, Render/Railway free tiers — they sleep and cannot keep Facebook browser sessions.

**Scheduling:** GitHub Actions cron triggers both jobs. You do not need cron on the worker itself.

---

## Architecture

```
GitHub schedule (8am / 12pm Chihuahua)
        │
        └─ sync job ──► fb-worker ──► scrape autosell.mx → diff → Playwright (Phase 2)
```

If fb-worker is offline: workflow queues or fails (GitHub emails you).

---

## Part A — GitHub secrets

**Settings → Secrets and variables → Actions**

| Secret | Example | Used by |
|--------|---------|---------|
| `AUTOSELL_BASE_URL` | `https://www.autosell.mx` | both jobs |
| `DRY_RUN` | `true` (now) → `false` (Phase 2) | fb-worker |
| `MAX_POSTS_PER_ACCOUNT_PER_RUN` | `10` | fb-worker |
| `TELEGRAM_BOT_TOKEN` | optional | fb-worker |
| `TELEGRAM_CHAT_ID` | optional | fb-worker |

Local dev: `cp .env.example .env`

---

## Part B — Oracle Linux VPS (free fb-worker)

### B1. Create VM

1. [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) signup (card required, stay in Always Free limits).
2. Home region: **Mexico Central** or **Mexico Northeast**.
3. Shape **VM.Standard.A1.Flex**: **2 OCPU**, **12 GB RAM**, Ubuntu 22.04 **ARM**, 50 GB disk.
4. “Out of host capacity” → retry another availability domain or off-peak hours.

```bash
ssh ubuntu@YOUR_VPS_IP
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl python3 python3-pip python3-venv
mkdir -p ~/auto-upload-data/data/snapshots ~/auto-upload-data/sessions
```

### B2. Register runner (label `fb-worker`)

GitHub → **Settings → Actions → Runners → New self-hosted runner → Linux → ARM64**

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# Download + extract using commands from GitHub UI

./config.sh \
  --url https://github.com/YOUR_USER/YOUR_REPO \
  --token YOUR_TOKEN \
  --labels fb-worker \
  --name oracle-fb-worker

sudo ./svc.sh install
sudo ./svc.sh start
```

Confirm runner is **Idle** with label **`fb-worker`**.

---

## Part C — Mac Mini (fb-worker)

Use when you have a dedicated Mac Mini that stays plugged in and awake.

### C1. macOS settings

- **System Settings → Energy**: prevent sleep on power adapter
- **Wake for network access**: on
- Optional: auto-login so runner starts after reboot

### C2. Install runner (label `fb-worker`)

GitHub → **New self-hosted runner → macOS → arm64** (Apple Silicon) or **x64** (Intel)

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
# Download + extract from GitHub UI

./config.sh \
  --url https://github.com/YOUR_USER/YOUR_REPO \
  --token YOUR_TOKEN \
  --labels fb-worker \
  --name mac-mini-fb-worker

./svc.sh install
./svc.sh start
```

```bash
mkdir -p ~/auto-upload-data/data/snapshots ~/auto-upload-data/sessions
brew install python@3.12   # if needed
```

**Only one fb-worker** should have the `fb-worker` label at a time (Oracle **or** Mac Mini, not both).

To switch later: stop/remove the old runner, register the new one with the same label.

---

## Part D — Run and verify

1. Push repo to GitHub with workflow file.
2. Add secrets from Part A.
3. Register fb-worker (Part B or C).
4. **Actions → Sync autosell → Facebook → Run workflow**

Expected:

- **sync** job: green on fb-worker — scrapes ~140 vehicles, prints create/update/remove plan

Until fb-worker is registered, the workflow will stay **queued** — this is expected.

Schedule: ~08:00 and ~12:00 America/Chihuahua (`0 14` and `0 18` UTC; adjust for DST).

---

## Part E — Facebook Marketplace (Playwright on fb-worker)

Facebook runs **only on fb-worker** (same machine as scrape). Use a persistent clone for manual work — not the ephemeral Actions checkout:

```bash
# Oracle VM example
ssh ubuntu@YOUR_VPS_IP
git clone https://github.com/YOUR_USER/auto-upload.git ~/auto-upload
cd ~/auto-upload
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
sudo .venv/bin/playwright install-deps chromium
```

Symlink persistent data (same layout as CI):

```bash
mkdir -p ~/auto-upload-data/data/snapshots ~/auto-upload-data/sessions
ln -snf ~/auto-upload-data/data ~/auto-upload/data
ln -snf ~/auto-upload-data/sessions ~/auto-upload/sessions
```

### E1. Log in once per account

Run on a machine with a display (local Arch/Mac) or X11-forwarded SSH. Headed login saves cookies to `sessions/account_N/`.

```bash
source .venv/bin/activate
python scripts/fb_login.py --account account_1
python scripts/fb_test_session.py --account account_1
```

Copy sessions to fb-worker if login was local:

```bash
scp -r sessions/account_1 ubuntu@YOUR_VPS_IP:~/auto-upload-data/sessions/
```

Repeat for `account_2` and `account_3`.

### E2. Test one listing (before live sync)

```bash
cd ~/auto-upload && source .venv/bin/activate
git pull origin main
python scripts/fb_post_test.py --account account_1 --autosell-id obj969 --max-photos 3
```

Expected log lines:

- `categorized: make=Audi, model=A3, ...`
- `verified make`, `verified model`
- `Posted: https://www.facebook.com/marketplace/item/...`

Confirm on [Marketplace → Your listings](https://www.facebook.com/marketplace/you/dashboard). New listings may show **“This listing is being reviewed”** for a short time — that is normal.

Find an existing listing URL by vehicle id:

```bash
python scripts/fb_find_listing.py --account account_1 --autosell-id obj969
```

Debug screenshots: `data/logs/facebook/{autosell_id}_*.png`

### E3. Autosell → Facebook field mapping

The poster fills FB’s vehicle composer in this order. Values come from the autosell catalog plus `src/facebook/categorize.py` when autosell has no equivalent field.

| Autosell / source | Facebook field | Notes |
|-------------------|----------------|-------|
| `brand` (Marca) | **Make** | Searchable dropdown — must click option (e.g. Audi) |
| `title` | **Model** | Text input; normalized (`A 3` → `A3`) |
| `year` | **Year** | Dropdown |
| `mileage` (Kilometraje) | **Mileage** | Digits only (`92,000 kms` → `92000`) |
| `price` (Precio) | **Price** | Digits only |
| `location_city` in config | **Location** | Default Chihuahua |
| inferred | **Vehicle type** | Usually `Car/Truck` |
| inferred | **Body style** | Sedan, SUV, Truck, Hatchback, … |
| inferred | **Exterior color** | Default Silver |
| inferred | **Interior color** | Default Black |
| inferred | **Fuel type** | Default Gasoline |
| inferred | **Transmission** | `Automatic transmission` |
| inferred | **Vehicle condition** | Default Excellent |
| — | **Clean title** | Checked |
| generated | **Description** | Title, km, specs, autosell URL |

Make/model/year must verify on the form before **Next** is enabled. The script only reports success after the listing URL matches **brand + price or model** on the item page (not year alone — avoids false matches from “Joined Facebook in 2020”).

### E4. Enable live sync

1. Verify manual post on each account.
2. Set GitHub secret `DRY_RUN=false`.
3. Monitor first scheduled runs; default cap is `MAX_POSTS_PER_ACCOUNT_PER_RUN=10` per run (~420 listings total at 3 accounts × ~140 vehicles).

```bash
# Full pipeline locally (respects DRY_RUN in .env)
python run_sync.py
```

---

## Local development (any machine)

Full pipeline (scrape + diff):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run_sync.py --dry-run
```

Split commands (same as CI):

```bash
python run_sync.py --scrape-only --output data/catalog_latest.json
python run_sync.py --from-snapshot data/catalog_latest.json --dry-run
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Workflow queued forever | Register fb-worker — autosell.mx cannot be scraped from GitHub cloud |
| Connect timeout to autosell.mx on ubuntu-latest | Expected — use fb-worker only (see workflow) |
| fb-worker offline | Start runner service on Oracle/Mac Mini |
| `sync.db` resets each run | Check symlinks to `~/auto-upload-data/data` in workflow |
| Oracle out of capacity | Retry AD/region; fallback Hetzner ~€4/mo |
| Facebook checkpoint | Re-login via `fb_login.py`; copy session to fb-worker |
| Next disabled on FB form | Check `data/logs/facebook/*_next_disabled*.png`; usually missing Make or vehicle details |
| Script prints wrong item URL | Dashboard is source of truth; `fb_find_listing.py` uses strict brand+price match |
| Listing “being reviewed” | Normal for new posts; appears on dashboard before public item page stabilizes |
| `pip install` blocked (Ubuntu 24.04) | Use project `.venv`, never system pip |

---

## Cost summary

| Component | Cost |
|-----------|------|
| GitHub Actions (orchestration) | free |
| Oracle fb-worker (scrape + FB) | $0 |
| Mac Mini fb-worker | ~$1–3/mo power |
| Paid VPS fallback | ~$5/mo |
| Hugging Face / Streamlit | not suitable |
