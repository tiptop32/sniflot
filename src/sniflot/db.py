"""SQLite connection and schema migrations (``PRAGMA user_version``)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1 — initial schema. Prices are integer kopecks, times are UTC ISO-8601.
    """
    CREATE TABLE listings (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        external_id TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        norm_title TEXT NOT NULL,
        price INTEGER,
        old_price INTEGER,
        currency TEXT NOT NULL DEFAULT 'RUB',
        available INTEGER,
        condition TEXT NOT NULL DEFAULT 'any',
        seller TEXT,
        location TEXT,
        image_url TEXT,
        brand TEXT,
        rating REAL,
        reviews INTEGER,
        published_at TEXT,
        status TEXT NOT NULL DEFAULT 'active',
        favorite INTEGER NOT NULL DEFAULT 0,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        last_checked_at TEXT,
        raw TEXT NOT NULL DEFAULT '{}',
        UNIQUE (source, external_id)
    );
    CREATE TABLE price_points (
        id INTEGER PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        at TEXT NOT NULL,
        price INTEGER,
        available INTEGER
    );
    CREATE INDEX price_points_listing ON price_points(listing_id, at);
    CREATE TABLE searches (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        query TEXT NOT NULL,
        sources TEXT NOT NULL,
        min_price INTEGER,
        max_price INTEGER,
        region TEXT,
        condition TEXT NOT NULL DEFAULT 'any',
        sort TEXT NOT NULL DEFAULT 'newest',
        exclude_words TEXT NOT NULL DEFAULT '[]',
        pages INTEGER NOT NULL DEFAULT 1,
        interval_minutes INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        notify_new INTEGER NOT NULL DEFAULT 1,
        notify_drop_pct REAL NOT NULL DEFAULT 5,
        baseline_done INTEGER NOT NULL DEFAULT 0,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        last_status TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE search_hits (
        search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
        listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        last_price INTEGER,
        PRIMARY KEY (search_id, listing_id)
    );
    CREATE TABLE watches (
        id INTEGER PRIMARY KEY,
        listing_id INTEGER NOT NULL UNIQUE REFERENCES listings(id) ON DELETE CASCADE,
        target_price INTEGER,
        drop_pct REAL NOT NULL DEFAULT 1,
        last_price INTEGER,
        last_available INTEGER,
        interval_minutes INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        last_status TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE source_state (
        source TEXT PRIMARY KEY,
        paused_until TEXT,
        failures INTEGER NOT NULL DEFAULT 0,
        last_ok_at TEXT,
        last_error TEXT,
        last_error_at TEXT
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        kind TEXT NOT NULL,
        listing_id INTEGER REFERENCES listings(id) ON DELETE SET NULL,
        search_id INTEGER REFERENCES searches(id) ON DELETE SET NULL,
        text TEXT NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0,
        error TEXT
    );
    CREATE INDEX events_at ON events(at);
    """,
]


class Database:
    """One shared connection guarded by a lock — enough for a single-user app."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;"
                )

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(
        self, sql: str, params: tuple[object, ...] | dict[str, object] = ()
    ) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(
        self, sql: str, params: tuple[object, ...] | dict[str, object] = ()
    ) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def execute(self, sql: str, params: tuple[object, ...] | dict[str, object] = ()) -> int:
        """Run a single statement; returns ``lastrowid``."""
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
