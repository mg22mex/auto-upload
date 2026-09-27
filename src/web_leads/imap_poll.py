"""IMAP polling + local Message-ID dedupe for webform notification emails."""
from __future__ import annotations

import email
import imaplib
import os
import sqlite3
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import Any, Iterator

from src.web_leads.parser import parse_email_message
from src.web_leads.models import WebLead

ENV_HOST = "WEB_LEADS_IMAP_HOST"
ENV_PORT = "WEB_LEADS_IMAP_PORT"
ENV_USER = "WEB_LEADS_IMAP_USER"
ENV_PASSWORD = "WEB_LEADS_IMAP_PASSWORD"
ENV_FOLDER = "WEB_LEADS_IMAP_FOLDER"
ENV_SEEN_DB = "WEB_LEADS_SEEN_DB"


@dataclass
class FetchedEmail:
    uid: str
    message: Message
    lead: WebLead


def seen_db_path() -> Path:
    raw = (os.getenv(ENV_SEEN_DB) or "").strip()
    if raw:
        return Path(raw)
    root = Path(__file__).resolve().parents[2]
    return root / "data" / "web_leads_seen.db"


def _connect_seen_db(path: Path | None = None) -> sqlite3.Connection:
    db = path or seen_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id TEXT PRIMARY KEY,
            lead_id INTEGER,
            phone TEXT,
            processed_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def already_processed(message_id: str, *, conn: sqlite3.Connection | None = None) -> bool:
    mid = (message_id or "").strip()
    if not mid:
        return False
    own = conn is None
    db = conn or _connect_seen_db()
    try:
        row = db.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ? LIMIT 1",
            (mid,),
        ).fetchone()
        return row is not None
    finally:
        if own:
            db.close()


def mark_processed(
    message_id: str,
    *,
    lead_id: int | None = None,
    phone: str = "",
    conn: sqlite3.Connection | None = None,
) -> None:
    mid = (message_id or "").strip()
    if not mid:
        return
    own = conn is None
    db = conn or _connect_seen_db()
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO processed_messages (message_id, lead_id, phone)
            VALUES (?, ?, ?)
            """,
            (mid, lead_id, phone),
        )
        db.commit()
    finally:
        if own:
            db.close()


def imap_configured() -> bool:
    return bool(
        (os.getenv(ENV_HOST) or "").strip()
        and (os.getenv(ENV_USER) or "").strip()
        and (os.getenv(ENV_PASSWORD) or "").strip()
    )


def fetch_unseen_web_leads(
    *,
    mark_seen: bool = True,
    limit: int = 50,
) -> list[FetchedEmail]:
    """Pull UNSEEN messages from the marketing mailbox and parse leads."""
    if not imap_configured():
        raise RuntimeError(
            f"IMAP not configured — set {ENV_HOST}, {ENV_USER}, {ENV_PASSWORD}"
        )
    host = (os.getenv(ENV_HOST) or "").strip()
    port = int((os.getenv(ENV_PORT) or "993").strip() or "993")
    user = (os.getenv(ENV_USER) or "").strip()
    password = (os.getenv(ENV_PASSWORD) or "").strip()
    folder = (os.getenv(ENV_FOLDER) or "INBOX").strip() or "INBOX"

    client = imaplib.IMAP4_SSL(host, port)
    try:
        client.login(user, password)
        typ, _ = client.select(folder)
        if typ != "OK":
            raise RuntimeError(f"IMAP select {folder!r} failed: {typ}")
        typ, data = client.search(None, "UNSEEN")
        if typ != "OK":
            raise RuntimeError(f"IMAP SEARCH UNSEEN failed: {typ}")
        ids = (data[0] or b"").split()
        out: list[FetchedEmail] = []
        for raw_id in ids[-max(1, int(limit)) :]:
            uid = raw_id.decode("ascii", errors="ignore")
            typ, fetched = client.fetch(raw_id, "(RFC822)")
            if typ != "OK" or not fetched:
                continue
            raw_bytes = b""
            for part in fetched:
                if isinstance(part, tuple) and len(part) >= 2:
                    raw_bytes = part[1]
                    break
            if not raw_bytes:
                continue
            msg = email.message_from_bytes(raw_bytes)
            lead = parse_email_message(msg)
            if already_processed(lead.message_id):
                if mark_seen:
                    client.store(raw_id, "+FLAGS", "\\Seen")
                continue
            out.append(FetchedEmail(uid=uid, message=msg, lead=lead))
            if mark_seen:
                client.store(raw_id, "+FLAGS", "\\Seen")
        return out
    finally:
        try:
            client.logout()
        except Exception:
            pass


__all__ = [
    "ENV_FOLDER",
    "ENV_HOST",
    "ENV_PASSWORD",
    "ENV_PORT",
    "ENV_SEEN_DB",
    "ENV_USER",
    "FetchedEmail",
    "already_processed",
    "fetch_unseen_web_leads",
    "imap_configured",
    "mark_processed",
    "seen_db_path",
]
