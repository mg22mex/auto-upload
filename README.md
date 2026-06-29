# Auto-upload

Sync [autosell.mx](https://www.autosell.mx) public catalog to Facebook Marketplace (Chihuahua, MX) across three personal accounts.

**Current status:** Phase 0–1 — scrape + diff on fb-worker (autosell.mx blocks GitHub cloud IPs). Facebook automation is Phase 2.

## Pipeline

| Job | Host | Action |
|-----|------|--------|
| **sync** | Self-hosted `fb-worker` | Scrape autosell.mx → diff → (Phase 2) Facebook |

> GitHub cloud runners cannot reach autosell.mx (connect timeout). You must register fb-worker before workflows succeed.

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
