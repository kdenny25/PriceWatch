"""SQLite storage. One shared connection guarded by a lock: plenty for a personal tool."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  query TEXT NOT NULL,
  sources TEXT NOT NULL,
  min_price REAL,
  max_price REAL,
  include_words TEXT,
  exclude_words TEXT,
  require_all_terms INTEGER NOT NULL DEFAULT 1,
  skip_junk INTEGER NOT NULL DEFAULT 1,
  auto_target INTEGER NOT NULL DEFAULT 1,
  target_percentile REAL NOT NULL DEFAULT 10,
  min_discount_pct REAL,
  interval_minutes INTEGER NOT NULL DEFAULT 30,
  enabled INTEGER NOT NULL DEFAULT 1,
  notify_channels TEXT,
  max_alerts_per_run INTEGER NOT NULL DEFAULT 5,
  created_at REAL NOT NULL,
  last_run_at REAL,
  next_run_at REAL NOT NULL DEFAULT 0,
  last_status TEXT,
  last_error TEXT,
  fail_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  price REAL NOT NULL,
  shipping REAL,
  total REAL NOT NULL,
  currency TEXT NOT NULL DEFAULT 'USD',
  url TEXT NOT NULL,
  image TEXT,
  location TEXT,
  condition TEXT,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  gone_at REAL,
  first_total REAL NOT NULL,
  notified INTEGER NOT NULL DEFAULT 0,
  UNIQUE (monitor_id, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_mon ON listings(monitor_id, active);
CREATE TABLE IF NOT EXISTS price_points (
  listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  n INTEGER NOT NULL,
  median REAL, p10 REAL, p25 REAL, p75 REAL, min REAL, mean REAL
);
CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(monitor_id, ts);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  source TEXT NOT NULL,
  total REAL NOT NULL,
  median REAL,
  reason TEXT,
  channels TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts ON alerts(monitor_id, ts);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  source TEXT NOT NULL,
  ok INTEGER NOT NULL,
  count INTEGER NOT NULL,
  error TEXT,
  ms INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

JSON_FIELDS = ("sources", "include_words", "exclude_words", "notify_channels")
BOOL_FIELDS = ("enabled", "require_all_terms", "skip_junk", "auto_target")
MONITOR_COLS = [
    "name", "query", "sources", "min_price", "max_price", "include_words",
    "exclude_words", "require_all_terms", "skip_junk", "auto_target",
    "target_percentile", "min_discount_pct", "interval_minutes", "enabled",
    "notify_channels", "max_alerts_per_run",
]


def hydrate(row: dict | None) -> dict | None:
    if row is None:
        return None
    r = dict(row)
    for f in JSON_FIELDS:
        raw = r.get(f)
        if raw:
            r[f] = json.loads(raw)
        else:
            r[f] = None if f == "notify_channels" else []
    for f in BOOL_FIELDS:
        r[f] = bool(r[f])
    return r


class DB:
    def __init__(self, path: str = ":memory:", clock=time.time):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA foreign_keys=ON")
        self.c.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            return self.c.execute(sql, args).lastrowid

    @contextmanager
    def tx(self):
        with self.lock:
            self.c.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.c.execute("ROLLBACK")
                raise
            else:
                self.c.execute("COMMIT")

    # ---- monitors -------------------------------------------------------
    def get_monitor(self, mid: int) -> dict | None:
        return hydrate(self.one("SELECT * FROM monitors WHERE id=?", (mid,)))

    def list_monitors(self) -> list[dict]:
        return [hydrate(r) for r in self.q("SELECT * FROM monitors ORDER BY id")]

    def save_monitor(self, data: dict, mid: int | None = None) -> int:
        vals = []
        for c in MONITOR_COLS:
            v = data[c]
            if c in JSON_FIELDS and v is not None:
                v = json.dumps(v)
            vals.append(v)
        now = self.clock()
        if mid is None:
            cols = ",".join(MONITOR_COLS + ["created_at", "next_run_at"])
            ph = ",".join("?" * (len(MONITOR_COLS) + 2))
            return self.x(f"INSERT INTO monitors({cols}) VALUES({ph})", vals + [now, now])
        sets = ",".join(f"{c}=?" for c in MONITOR_COLS)
        self.x(f"UPDATE monitors SET {sets}, next_run_at=?, fail_count=0 WHERE id=?", vals + [now, mid])
        return mid
