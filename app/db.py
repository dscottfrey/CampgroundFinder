"""SQLite connection + schema (§8). Stdlib `sqlite3` only — no ORM."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

#: Anchored to the repo, not the current directory. A cwd-relative default
#: meant that running from the wrong place created a second, empty database
#: somewhere else and reported nothing wrong.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "campgroundfinder.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campgrounds (
  provider TEXT, id TEXT,
  name TEXT, rec_area TEXT, state TEXT, latitude REAL, longitude REAL,
  reservation_type TEXT,
  status TEXT,
  status_reason TEXT, closed_until TEXT,
  first_cataloged TEXT, last_checked TEXT,
  seeded INTEGER DEFAULT 0,      -- part of the committed completeness floor (§8k)
  -- Where this coordinate came from, and when. A point from a province's own
  -- open-data API and one parsed off a booking page are not the same claim,
  -- and neither is a guess — which must never appear here at all.
  coord_source TEXT,
  coord_updated TEXT,
  -- Does a reservable campground ALSO hold first-come sites? Three-state (§8g):
  -- 1 yes, 0 no, NULL unknown. Distinct from reservation_type, which says what
  -- the campground as a whole is.
  first_come_sites INTEGER,
  -- Site counts from the provider's own inventory, where it has one.
  -- `sites_not_bookable` deliberately means "not bookable online" and NOT
  -- "first-come" — see docs/first-come-research.md. Management/host pitches
  -- are excluded from both figures.
  sites_total INTEGER,
  sites_not_bookable INTEGER,
  -- CampsiteType -> {bookable, not_bookable}, as JSON. Captured from the same
  -- request as the counts so the ACCESS question (hike-in, boat-in,
  -- equestrian, tent-only) never needs a second 545-request pass. Access mode
  -- and booking mode are different axes — see docs/terminology.md.
  site_types TEXT,
  -- How far the driveway figures at THIS campground can be trusted:
  -- 'measured' | 'default' | 'unknown'. A property of the campground, never of
  -- the provider — the same platform carries both (docs/terminology.md).
  length_data_quality TEXT,
  inventory_source TEXT,
  inventory_updated TEXT,
  -- Is there water here? DERIVED, because nobody states it: ReserveAmerica's
  -- own `Near Water` field is `no` on all 5,313 Oregon sites that carry it
  -- (app/water.py). Two states only, 'yes' | 'unknown' — never 'no', because
  -- a lakeside campground with a dull name is unknown, not dry. The one
  -- exception is a human verdict in the curated file, which may say 'no'
  -- because a person looking at a map is real evidence.
  water_nearby TEXT,
  -- Why `water_nearby` says what it says, in words a camper could read:
  -- "named for Diamond Lake", "the operator lists Boating, Swimming",
  -- "checked on a map by Scott". A derived flag that can't say why it fired
  -- is the confident guess this project keeps getting punished for.
  water_evidence TEXT,
  -- Operator-listed activities, JSON array. Fetched with the facility record,
  -- so the water question never costs its own pass.
  activities TEXT,
  -- A photo of the campground and the operator's own description. Both come
  -- from the same request as the activities.
  photo_url TEXT,
  photo_credit TEXT,
  description TEXT,
  PRIMARY KEY (provider, id)
);

-- Operator notices: burn bans, closures, water quality (app/alerts.py).
-- Refreshed daily and REPLACED wholesale per provider, because an alert that
-- has been taken down is an alert that no longer applies — merging would
-- leave a lifted burn ban on the map forever.
CREATE TABLE IF NOT EXISTS park_alerts (
  provider TEXT, campground_id TEXT,
  alert_type TEXT,               -- 'Burn Ban' | 'Park is Completely Closed' | …
  level TEXT,                    -- burn bans only: '1'..'4' | 'no fires at any time'
  posted TEXT,                   -- when the operator posted it; a 2024 date is
                                 -- a standing rule, not a current emergency
  text TEXT,                     -- the operator's own words, shown verbatim
  updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_campground
  ON park_alerts (provider, campground_id);

-- Per-site inventory: what EXISTS at a campground, as opposed to what is open
-- on a date. Measured once by a backfill and then left alone — a campground's
-- sites change on the order of years (app/inventory.py).
CREATE TABLE IF NOT EXISTS campsites (
  provider TEXT, campground_id TEXT, site_id TEXT,
  name TEXT, loop TEXT, site_type TEXT, type_of_use TEXT,
  reservable INTEGER,
  -- 'not bookable online', NOT 'first-come' — see docs/first-come-research.md
  max_vehicle_length INTEGER,     -- NULL when unstated; 0 upstream means n/a
  site_access TEXT,               -- raw, as the source spelled it
  access_class TEXT,              -- normalized: 'hike_in' | 'drive_in' | NULL
  driveway_entry TEXT,            -- 'Back-In' | 'Pull-Through'
  max_people INTEGER,
  accessible INTEGER,
  latitude REAL, longitude REAL,  -- some sources give per-SITE coordinates
  permitted_equipment TEXT,       -- JSON
  attributes TEXT,                -- JSON: everything else the source stated
  source TEXT, updated TEXT,
  PRIMARY KEY (provider, campground_id, site_id)
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
CREATE INDEX IF NOT EXISTS idx_campsites_campground
  ON campsites (provider, campground_id);
CREATE INDEX IF NOT EXISTS idx_campsites_access ON campsites (access_class);
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


#: Columns added after the first release. `CREATE TABLE IF NOT EXISTS` will not
#: add a column to a table that already exists, so an existing database needs
#: these applied explicitly — otherwise the app runs fine until the first query
#: that names one.
MIGRATIONS = [
    ("campgrounds", "coord_source", "TEXT"),
    ("campgrounds", "coord_updated", "TEXT"),
    ("campgrounds", "first_come_sites", "INTEGER"),
    ("campgrounds", "sites_total", "INTEGER"),
    ("campgrounds", "sites_not_bookable", "INTEGER"),
    ("campgrounds", "site_types", "TEXT"),
    ("campgrounds", "length_data_quality", "TEXT"),
    ("campsites", "access_class", "TEXT"),
    ("campgrounds", "inventory_source", "TEXT"),
    ("campgrounds", "inventory_updated", "TEXT"),
    ("campgrounds", "water_nearby", "TEXT"),
    ("campgrounds", "water_evidence", "TEXT"),
    ("campgrounds", "activities", "TEXT"),
    ("campgrounds", "photo_url", "TEXT"),
    ("campgrounds", "photo_credit", "TEXT"),
    ("campgrounds", "description", "TEXT"),
]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any missing columns. Idempotent; returns what it applied."""
    applied = []
    for table, column, decl in MIGRATIONS:
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing or column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        applied.append(f"{table}.{column}")
    if applied:
        conn.commit()
    return applied


def init_schema(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def open_db(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    return init_schema(connect(path))
