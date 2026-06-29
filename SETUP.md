# Setup: GitHub + fb-worker (Oracle VPS or Mac Mini)

# Split architecture (updated)

**Important:** autosell.mx does **not** respond to GitHub cloud runner IPs (connect timeout). Scrape and Facebook both run on **fb-worker** — not `ubuntu-latest`.

| Step | Host | Does |
|------|------|------|
| **sync** job | **fb-worker** | Scrape → diff → (Phase 2) Facebook |

Your daily PC can stay off. Only **fb-worker** must be always on and registered before workflows can run.

Phase 0–1 (current): scrape + diff planning. Facebook automation is Phase 2.

---

## Where data lives

| Path | Location | In git? |
|------|----------|---------|
| `data/catalog_latest.json` | Written on fb-worker each run; optional GitHub artifact | No |
| `data/sync.db` | **`~/auto-upload-data/data/` on fb-worker** | No |
| `sessions/account_*` | **`~/auto-upload-data/sessions/` on fb-worker** | No |
| `data/snapshots/` | Archived on fb-worker after each run | No |

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

## Part E — Phase 2 Facebook (on fb-worker only)

On the fb-worker machine (Ubuntu 24.04 — use a venv, not system `pip`):

```bash
cd ~/actions-runner/_work/auto-upload/auto-upload   # or your clone path
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# sudo does not see venv binaries — use the full path:
sudo .venv/bin/playwright install-deps chromium
```

Log in once per Facebook account (headed browser; SSH with X11 forwarding or run from a machine that can open a display):

```bash
source .venv/bin/activate
python scripts/fb_login.py --account account_1
python scripts/fb_test_session.py --account account_1
```

Sessions persist under `~/auto-upload-data/sessions/account_1` (symlinked as `sessions/` in CI).

Test one listing before enabling live sync:

```bash
python scripts/fb_post_test.py --account account_1 --autosell-id obj969
```

Set `DRY_RUN=false` in GitHub Secrets only after a manual post succeeds.

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
| Facebook checkpoint | Re-login on fb-worker via Screen Sharing (Mac) or SSH (Linux) |

---

## Cost summary

| Component | Cost |
|-----------|------|
| GitHub Actions (orchestration) | free |
| Oracle fb-worker (scrape + FB) | $0 |
| Mac Mini fb-worker | ~$1–3/mo power |
| Paid VPS fallback | ~$5/mo |
| Hugging Face / Streamlit | not suitable |
