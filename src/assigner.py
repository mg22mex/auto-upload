"""Persistent round-robin cursor for branch sales-rep rotation.

In-process ``_cursor`` alone resets to 0 on every deploy/restart. This module
stores the next-index per branch in SQLite so rotation survives process death.

Default DB: ``data/rr_cursor.db`` (override with ``RR_CURSOR_DB_PATH``).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

ENV_RR_CURSOR_DB = "RR_CURSOR_DB_PATH"
DEFAULT_RR_CURSOR_DB = "data/rr_cursor.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rr_cursor (
    branch TEXT NOT NULL PRIMARY KEY,
    next_index INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_lock = threading.Lock()
_path_cache: str | None = None


def rr_cursor_db_path() -> Path:
    raw = (os.getenv(ENV_RR_CURSOR_DB) or DEFAULT_RR_CURSOR_DB).strip()
    return Path(raw or DEFAULT_RR_CURSOR_DB)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    global _path_cache
    db = path or rr_cursor_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    _path_cache = str(db)
    return conn


def load_cursors(path: Path | None = None) -> dict[str, int]:
    """Read all branch → next_index values from disk."""
    with _lock:
        try:
            conn = _connect(path)
            try:
                rows = conn.execute(
                    "SELECT branch, next_index FROM rr_cursor"
                ).fetchall()
            finally:
                conn.close()
        except Exception as exc:
            print(f"WARN rr_cursor load failed: {type(exc).__name__}: {exc}", flush=True)
            return {}
    out: dict[str, int] = {}
    for branch, idx in rows:
        key = str(branch or "").strip()
        if not key:
            continue
        try:
            out[key] = max(0, int(idx))
        except (TypeError, ValueError):
            continue
    return out


def save_cursor(branch: str, next_index: int, *, path: Path | None = None) -> None:
    """Upsert one branch cursor (next index to pick)."""
    key = str(branch or "").strip()
    if not key:
        return
    try:
        idx = max(0, int(next_index))
    except (TypeError, ValueError):
        return
    with _lock:
        try:
            conn = _connect(path)
            try:
                conn.execute(
                    "INSERT INTO rr_cursor (branch, next_index, updated_at) "
                    "VALUES (?, ?, datetime('now')) "
                    "ON CONFLICT(branch) DO UPDATE SET "
                    "next_index=excluded.next_index, "
                    "updated_at=excluded.updated_at",
                    (key, idx),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(
                f"WARN rr_cursor save failed branch={key!r}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )


def clear_cursors(branch: str | None = None, *, path: Path | None = None) -> None:
    """Delete one branch cursor or wipe the whole table."""
    with _lock:
        try:
            conn = _connect(path)
            try:
                if branch is None:
                    conn.execute("DELETE FROM rr_cursor")
                else:
                    key = str(branch).strip()
                    if key:
                        conn.execute(
                            "DELETE FROM rr_cursor WHERE branch = ?", (key,)
                        )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f"WARN rr_cursor clear failed: {type(exc).__name__}: {exc}", flush=True)


def cursor_snapshot(*, path: Path | None = None) -> dict[str, Any]:
    """Debug helper: path + current cursors."""
    db = path or rr_cursor_db_path()
    return {"path": str(db), "cursors": load_cursors(db)}


__all__ = [
    "DEFAULT_RR_CURSOR_DB",
    "ENV_RR_CURSOR_DB",
    "clear_cursors",
    "cursor_snapshot",
    "load_cursors",
    "rr_cursor_db_path",
    "save_cursor",
]
