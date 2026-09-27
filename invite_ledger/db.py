"""SQLite 连接与事务管理。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id TEXT NOT NULL,
    deadline_hours INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    cancelled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
    influencer_id TEXT NOT NULL,
    brand_id TEXT NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    resolved_at TEXT,
    idempotency_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    brand_id TEXT NOT NULL,
    key TEXT NOT NULL,
    campaign_id INTEGER NOT NULL,
    influencer_id TEXT NOT NULL,
    result_ok INTEGER NOT NULL,
    result_invitation_id INTEGER,
    result_reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (brand_id, key)
);
"""


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """打开（必要时创建）数据库连接，并保证表结构存在。"""
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def immediate_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """以 BEGIN IMMEDIATE 开启事务，提交或回滚，保证多进程下串行化写。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
