"""SQLite persistence: tag registry (boxes), confirmed readings, chunk index.

Concurrency model
-----------------
Every check-then-write operation runs inside a single ``BEGIN IMMEDIATE``
transaction.  SQLite grants the write lock at ``BEGIN IMMEDIATE``, so
concurrent registrations/scans serialize rather than racing between check
and insert.  A process-wide re-entrant lock in :mod:`app.services`
additionally serializes the FastAPI thread-pool workers in the supported
single-worker deployment (see ``docker-compose.yml``).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS boxes (
    box_number TEXT PRIMARY KEY,
    grid_key   TEXT NOT NULL UNIQUE,
    grid_json  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_no  TEXT NOT NULL UNIQUE,
    grid_key    TEXT NOT NULL UNIQUE,
    grid_json   TEXT NOT NULL,
    box_number  TEXT NOT NULL REFERENCES boxes(box_number),
    orientation INTEGER NOT NULL,
    mismatches  INTEGER NOT NULL,
    visible     INTEGER NOT NULL,
    scanner_id  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_readings_box ON readings(box_number);

-- Chunk signature index that makes registration conflict checks avoid a
-- full scan of confirmed readings.  The leading columns serve lookups of
-- the form: WHERE chunk_index = ? AND bits = ?.
CREATE TABLE IF NOT EXISTS reading_chunks (
    chunk_index INTEGER NOT NULL,
    bits        TEXT NOT NULL,
    reading_id  INTEGER NOT NULL REFERENCES readings(id) ON DELETE CASCADE,
    PRIMARY KEY (chunk_index, bits, reading_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_reading_chunks_reading
    ON reading_chunks(reading_id);
"""


def database_path() -> Path:
    raw = os.environ.get("DB_PATH", "data/exhibit.db")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    # isolation_level=None -> autocommit; we manage BEGIN IMMEDIATE ourselves.
    conn = sqlite3.connect(
        str(database_path()),
        timeout=float(os.environ.get("DB_TIMEOUT", "15")),
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Yield inside a ``BEGIN IMMEDIATE`` transaction; commit/rollback."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
