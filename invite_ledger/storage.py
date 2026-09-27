"""SQLite persistence layer. All multi-step writes run in IMMEDIATE transactions."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id TEXT NOT NULL,
    accept_ttl_hours INTEGER NOT NULL,
    created_at REAL NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    brand_id TEXT NOT NULL,
    talent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at REAL NOT NULL,
    deadline REAL NOT NULL,
    responded_at REAL,
    expired_at REAL
);
CREATE INDEX IF NOT EXISTS idx_invitations_campaign ON invitations(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_invitations_talent ON invitations(campaign_id, talent_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    brand_id TEXT NOT NULL,
    key TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    talent_id TEXT NOT NULL,
    first_used_at REAL NOT NULL,
    result_ok INTEGER NOT NULL,
    result_invitation_id INTEGER,
    result_error TEXT,
    PRIMARY KEY (brand_id, key)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialize writers across processes via an IMMEDIATE write lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
