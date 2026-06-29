from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.models import Vehicle


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SyncStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalog_snapshots (
                autosell_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                brand TEXT NOT NULL,
                year TEXT NOT NULL,
                price TEXT NOT NULL,
                mileage TEXT NOT NULL,
                version TEXT NOT NULL,
                url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                image_urls_json TEXT NOT NULL,
                specs_json TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS fb_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                autosell_id TEXT NOT NULL,
                account_id TEXT NOT NULL,
                fb_listing_url TEXT,
                status TEXT NOT NULL,
                content_hash TEXT,
                posted_at TEXT,
                updated_at TEXT,
                UNIQUE (autosell_id, account_id)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                dry_run INTEGER NOT NULL,
                vehicles_found INTEGER NOT NULL DEFAULT 0,
                creates INTEGER NOT NULL DEFAULT 0,
                updates INTEGER NOT NULL DEFAULT 0,
                removals INTEGER NOT NULL DEFAULT 0,
                notes TEXT
            );
            """
        )
        self._conn.commit()

    def upsert_catalog_snapshot(self, vehicle: Vehicle) -> None:
        import json

        now = utc_now()
        self._conn.execute(
            """
            INSERT INTO catalog_snapshots (
                autosell_id, slug, title, brand, year, price, mileage, version,
                url, content_hash, image_urls_json, specs_json, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(autosell_id) DO UPDATE SET
                slug = excluded.slug,
                title = excluded.title,
                brand = excluded.brand,
                year = excluded.year,
                price = excluded.price,
                mileage = excluded.mileage,
                version = excluded.version,
                url = excluded.url,
                content_hash = excluded.content_hash,
                image_urls_json = excluded.image_urls_json,
                specs_json = excluded.specs_json,
                last_seen_at = excluded.last_seen_at
            """,
            (
                vehicle.autosell_id,
                vehicle.slug,
                vehicle.title,
                vehicle.brand,
                vehicle.year,
                vehicle.price,
                vehicle.mileage,
                vehicle.version,
                vehicle.url,
                vehicle.content_hash(),
                json.dumps(vehicle.image_urls, ensure_ascii=False),
                json.dumps(vehicle.specs, ensure_ascii=False),
                now,
            ),
        )

    def commit_catalog_snapshot(self, active_ids: set[str]) -> None:
        if not active_ids:
            return
        placeholders = ",".join("?" for _ in active_ids)
        self._conn.execute(
            f"""
            DELETE FROM catalog_snapshots
            WHERE autosell_id NOT IN ({placeholders})
            """,
            tuple(active_ids),
        )
        self._conn.commit()

    def get_live_listings(self) -> list[sqlite3.Row]:
        cursor = self._conn.execute(
            """
            SELECT autosell_id, account_id, fb_listing_url, status, content_hash
            FROM fb_listings
            WHERE status = 'live'
            """
        )
        return cursor.fetchall()

    def start_sync_run(self, dry_run: bool) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO sync_runs (started_at, dry_run)
            VALUES (?, ?)
            """,
            (utc_now(), 1 if dry_run else 0),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        *,
        vehicles_found: int,
        creates: int,
        updates: int,
        removals: int,
        notes: str = "",
    ) -> None:
        self._conn.execute(
            """
            UPDATE sync_runs
            SET finished_at = ?, vehicles_found = ?, creates = ?, updates = ?,
                removals = ?, notes = ?
            WHERE id = ?
            """,
            (utc_now(), vehicles_found, creates, updates, removals, notes, run_id),
        )
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()
