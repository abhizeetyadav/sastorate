"""SQLite storage. Only *changes* are stored: if a store's price for a variant is
unchanged since the last run we just bump `last_seen`. That keeps the database
tiny (it is committed to git by the GitHub Action) while preserving full history.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_points (
    id          INTEGER PRIMARY KEY,
    store_id    TEXT NOT NULL,
    variant_key TEXT NOT NULL,
    model       TEXT NOT NULL,
    series      TEXT,
    storage_gb  INTEGER,
    title       TEXT,
    url         TEXT,
    price       INTEGER NOT NULL,
    old_price   INTEGER,
    in_stock    INTEGER,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pp_store_variant ON price_points(store_id, variant_key, last_seen);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    store_id    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    offers      INTEGER DEFAULT 0,
    pages       INTEGER DEFAULT 0,
    error       TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(price_points)")}
        if "image" not in cols:  # upgrade databases created before images were collected
            self.conn.execute("ALTER TABLE price_points ADD COLUMN image TEXT")
            self.conn.commit()

    def start_run(self, store_id: str) -> int:
        cur = self.conn.execute("INSERT INTO runs(store_id, started_at, status) VALUES (?,?, 'running')",
                                (store_id, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, offers: int = 0, pages: int = 0, error: str | None = None):
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, offers=?, pages=?, error=? WHERE id=?",
                          (now_iso(), status, offers, pages, (error or "")[:500] or None, run_id))
        self.conn.commit()

    def record(self, store_id: str, variant, title: str, url: str, price: int,
               old_price: int | None, in_stock: bool | None, seen_at: str | None = None,
               image: str | None = None) -> bool:
        """Insert a new price point if anything changed. Returns True if price changed."""
        seen_at = seen_at or now_iso()
        last = self.conn.execute(
            "SELECT id, price, old_price, in_stock FROM price_points WHERE store_id=? AND variant_key=? "
            "ORDER BY last_seen DESC, id DESC LIMIT 1", (store_id, variant.key)).fetchone()
        stock = None if in_stock is None else int(in_stock)
        if last and last["price"] == price and last["old_price"] == old_price and last["in_stock"] == stock:
            self.conn.execute("UPDATE price_points SET last_seen=?, url=?, title=?, image=COALESCE(?, image) WHERE id=?",
                              (seen_at, url, title, image, last["id"]))
            return False
        self.conn.execute(
            "INSERT INTO price_points(store_id, variant_key, model, series, storage_gb, title, url, price, "
            "old_price, in_stock, first_seen, last_seen, image) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (store_id, variant.key, variant.model, variant.series, variant.storage_gb, title, url, price,
             old_price, stock, seen_at, seen_at, image))
        return bool(last and last["price"] != price)

    def commit(self):
        self.conn.commit()

    def q(self, sql: str, *args):
        return self.conn.execute(sql, args).fetchall()
