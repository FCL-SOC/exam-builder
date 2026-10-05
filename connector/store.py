"""
Short links: each exam the connector makes is kept for LINK_DAYS so its link can be short.

A link that carried the whole exam would be 5–20 KB of base64 that Claude has to copy into its reply character
for character, and it gets that wrong. So the connector keeps the packed exam here under a 12-character id;
/e/<id> then hands the browser a #data= link, and the exam itself never touches GitHub's servers.

Nothing here identifies a teacher: a row is an id, the packed exam, a summary for the landing page and two times.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id         TEXT PRIMARY KEY,
    packed     TEXT NOT NULL,
    summary    TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_expiry ON links(expires_at);
"""


class LinkStore:
    def __init__(self, path: Path, days: float = 30) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = days * 86400
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")

    def put(self, packed: str, summary: dict) -> tuple[str, float]:
        """Store a packed exam; returns (id, expires_at)."""
        now = time.time()
        link_id = secrets.token_urlsafe(9)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM links WHERE expires_at < ?", (now,))
            self._conn.execute("INSERT INTO links VALUES (?, ?, ?, ?, ?)",
                               (link_id, packed, json.dumps(summary), now, now + self.ttl))
        return link_id, now + self.ttl

    def get(self, link_id: str) -> tuple[str, dict, float] | None:
        """(packed, summary, expires_at), or None if unknown or expired."""
        with self._lock:
            row = self._conn.execute("SELECT packed, summary, expires_at FROM links WHERE id = ? AND expires_at >= ?",
                                     (link_id, time.time())).fetchone()
        return (row[0], json.loads(row[1]), row[2]) if row else None

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM links WHERE expires_at >= ?", (time.time(),)).fetchone()[0]
