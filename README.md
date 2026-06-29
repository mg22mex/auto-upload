# Auto-upload

Sync [autosell.mx](https://www.autosell.mx) public catalog to Facebook Marketplace (Chihuahua, MX) across three personal accounts.

**Current status:** Phase 0–1 — split CI pipeline (scrape on GitHub cloud, diff on fb-worker). Facebook automation is Phase 2.

## Pipeline

| Job | Host | Command |
|-----|------|---------|
| **scrape** | GitHub `ubuntu-latest` | `python run_sync.py --scrape-only` |
| **facebook-sync** | Self-hosted `fb-worker` | `python run_sync.py --from-snapshot data/catalog_latest.json` |

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run_sync.py --dry-run
```

## Production setup

1. Push to GitHub and add secrets (see `.env.example`).
2. Register a self-hosted runner with label **`fb-worker`** on Oracle free VPS **or** Mac Mini.
3. See **[SETUP.md](./SETUP.md)** for step-by-step instructions.

Persistent state on fb-worker: `~/auto-upload-data/data/sync.db` and `~/auto-upload-data/sessions/`.

## Schedule

Twice daily (~08:00 and ~12:00 America/Chihuahua) plus manual **Run workflow**.
