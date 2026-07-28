"""SQLite connection + schema (§8). Stdlib `sqlite3` only — no ORM."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("data/campgroundfinder.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS campgrounds (
  provider TEXT, id TEXT,
  name TEXT, rec_area TEXT, state TEXT, latitude REAL, longitude REAL,
  reservation_type TEXT,
  status TEXT,
  status_reason TEXT, closed_until TEXT,
  first_cataloged TEXT, last_checked TEXT,
  seeded INTEGER DEFAULT 0,      -- part of the committed completeness floor (§8k)
  PRIMARY KEY (provider, id)
);

CREATE TABLE IF NOT EXISTS availability (
  key TEXT PRIMARY KEY,
  provider TEXT, campsite_id TEXT, available_date TEXT, nights INTEGER,
  site_name TEXT, loop TEXT, campsite_type TEXT, status TEXT,
  reservation_type TEXT,
  rec_area TEXT, rec_area_id TEXT, facility_name TEXT, facility_id TEXT,
  booking_url TEXT, state TEXT, latitude REAL, longitude REAL, extra TEXT,
  aqi_status TEXT,
  fire_status TEXT,
  first_seen TEXT, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, ts_login TEXT UNIQUE, email TEXT, name TEXT,
  role TEXT,
  status TEXT,
  pw_hash TEXT,
  notify_targets TEXT, created TEXT
);

CREATE TABLE IF NOT EXISTS watches (
  id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, provider TEXT,
  mode TEXT,
  rec_area_ids TEXT, campground_ids TEXT, campsite_ids TEXT,
  start_date TEXT, end_date TEXT, nights INTEGER, weekends_only INTEGER,
  filters TEXT, notify_targets TEXT, active INTEGER, created TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, watch_id INTEGER, campsite_key TEXT, sent_at TEXT
);

-- What the scanner is doing right now, so the interface can explain a wait in
-- plain language instead of showing a bare spinner (docs/scanning-design.md).
-- Exactly one row: the scanner is a single sequential worker by design.
CREATE TABLE IF NOT EXISTS scan_status (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT,                    -- idle | scanning | waiting | blocked
  provider TEXT,
  target TEXT,                   -- what's being checked right now
  done INTEGER, total INTEGER,
  message TEXT,                  -- ready to display, no jargon
  detail TEXT,                   -- why we're waiting or backing off
  started TEXT, updated TEXT
);

CREATE INDEX IF NOT EXISTS idx_avail_provider_date
  ON availability (provider, available_date);
CREATE INDEX IF NOT EXISTS idx_avail_facility
  ON availability (provider, facility_id);
CREATE INDEX IF NOT EXISTS idx_avail_last_seen
  ON availability (last_seen);
CREATE INDEX IF NOT EXISTS idx_campgrounds_state ON campgrounds (state);
CREATE INDEX IF NOT EXISTS idx_notifications_lookup
  ON notifications (watch_id, campsite_key, sent_at);
"""


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open (and create if needed) the database. Pass ":memory:" for tests."""
    if path is None:
        path = os.environ.get("CAMPGROUNDFINDER_DB", DEFAULT_DB_PATH)
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def open_db(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    return init_schema(connect(path))
