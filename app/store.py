"""Persistence + query layer over SQLite (§8).

Two distinct things live here, and the distinction matters (§8k):
  * `campgrounds` — the catalog: the known universe. Never shrinks.
  * `availability` — a cache of what's currently open. Pruned aggressively.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from .pacing import PACING_NOTE
from .providers.base import (
    STATUS_AVAILABLE,
    STATUS_FULL,
    STATUS_STALE,
    STATUS_UNKNOWN,
    Campground,
    Campsite,
)
from .util import dumps, haversine_miles, is_weekend_night, iso, loads, parse_iso, to_date, utcnow

DEFAULT_RENOTIFY_COOLDOWN_HOURS = 8  # §8b: 6–12h so a flapping site doesn't spam


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------

def upsert_availability(
    conn: sqlite3.Connection,
    campsites: Iterable[Campsite],
    now: Optional[datetime] = None,
) -> list[Campsite]:
    """Insert/refresh availability rows. Returns only the NEWLY-appeared ones.

    "Newly appeared" is what drives alerts (§8b) — a row we already had and
    merely re-confirmed this cycle must not re-notify.
    """
    stamp = iso(now)
    new: list[Campsite] = []
    for site in campsites:
        existing = conn.execute(
            "SELECT key FROM availability WHERE key = ?", (site.key,)
        ).fetchone()
        row = (
            site.key, site.provider, site.campsite_id, str(site.available_date),
            site.nights, site.site_name, site.loop, site.campsite_type, site.status,
            site.reservation_type, site.rec_area, site.rec_area_id, site.facility_name,
            site.facility_id, site.booking_url, site.state, site.latitude, site.longitude,
            dumps({"attributes": site.attributes, **site.extra}),
            site.aqi_status, site.fire_status,
        )
        if existing:
            conn.execute(
                """UPDATE availability SET
                     provider=?, campsite_id=?, available_date=?, nights=?, site_name=?,
                     loop=?, campsite_type=?, status=?, reservation_type=?, rec_area=?,
                     rec_area_id=?, facility_name=?, facility_id=?, booking_url=?, state=?,
                     latitude=?, longitude=?, extra=?, aqi_status=?, fire_status=?,
                     last_seen=?
                   WHERE key=?""",
                row[1:] + (stamp, site.key),
            )
        else:
            conn.execute(
                """INSERT INTO availability (
                     key, provider, campsite_id, available_date, nights, site_name,
                     loop, campsite_type, status, reservation_type, rec_area, rec_area_id,
                     facility_name, facility_id, booking_url, state, latitude, longitude,
                     extra, aqi_status, fire_status, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row + (stamp, stamp),
            )
            new.append(site)
    conn.commit()
    return new


def prune_availability(
    conn: sqlite3.Connection,
    older_than: datetime,
) -> int:
    """Drop availability rows not re-confirmed since `older_than`.

    The campground stays in the catalog and drops to full/unknown — only the
    *availability* row goes (§8 cycle step 3).
    """
    cur = conn.execute(
        "DELETE FROM availability WHERE last_seen < ?", (iso(older_than),)
    )
    conn.commit()
    return cur.rowcount


def row_to_campsite(row: sqlite3.Row) -> Campsite:
    blob = loads(row["extra"], {})
    attributes = blob.pop("attributes", {}) or {}
    return Campsite(
        provider=row["provider"],
        campsite_id=row["campsite_id"],
        available_date=to_date(row["available_date"]),
        nights=row["nights"],
        site_name=row["site_name"],
        loop=row["loop"],
        campsite_type=row["campsite_type"],
        status=row["status"],
        reservation_type=row["reservation_type"] or "reservable",
        rec_area=row["rec_area"],
        rec_area_id=row["rec_area_id"],
        facility_name=row["facility_name"],
        facility_id=row["facility_id"],
        booking_url=row["booking_url"],
        state=row["state"],
        aqi_status=row["aqi_status"],
        fire_status=row["fire_status"],
        attributes=attributes,
        latitude=row["latitude"],
        longitude=row["longitude"],
        extra=blob,
    )


def list_availability(
    conn: sqlite3.Connection,
    provider: Optional[str] = None,
    states: Optional[Iterable[str]] = None,
    facility_id: Optional[str] = None,
) -> list[Campsite]:
    sql = "SELECT * FROM availability WHERE 1=1"
    params: list[Any] = []
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    if facility_id:
        sql += " AND facility_id = ?"
        params.append(facility_id)
    states = list(states or [])
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY available_date, provider, campsite_id"
    return [row_to_campsite(r) for r in conn.execute(sql, params)]


# --------------------------------------------------------------------------
# catalog (§8k)
# --------------------------------------------------------------------------

def upsert_campgrounds(
    conn: sqlite3.Connection,
    campgrounds: Iterable[Campground],
    seeded: bool = False,
    now: Optional[datetime] = None,
) -> tuple[int, int]:
    """Add/update catalog rows. Returns (added, updated). Never deletes."""
    stamp = iso(now)
    added = updated = 0
    for cg in campgrounds:
        existing = conn.execute(
            "SELECT provider, id, seeded FROM campgrounds WHERE provider=? AND id=?",
            (cg.provider, cg.id),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE campgrounds SET
                     name=?, rec_area=?, state=?, latitude=?, longitude=?,
                     reservation_type=?, status=?, status_reason=?, closed_until=?,
                     last_checked=?, seeded=?,
                     coord_source=COALESCE(?, coord_source),
                     first_come_sites=COALESCE(?, first_come_sites),
                     sites_total=COALESCE(?, sites_total),
                     sites_not_bookable=COALESCE(?, sites_not_bookable),
                     length_data_quality=COALESCE(?, length_data_quality)
                   WHERE provider=? AND id=?""",
                (
                    cg.name, cg.rec_area, cg.state, cg.latitude, cg.longitude,
                    cg.reservation_type, cg.status, cg.status_reason, cg.closed_until,
                    stamp, 1 if (seeded or existing["seeded"]) else 0,
                    # COALESCE, not a plain assignment: a routine enumeration
                    # carries no provenance and must not erase a recorded one.
                    cg.coord_source,
                    # COALESCE again: an enumeration that cannot tell must not
                    # flip a known answer back to "unknown".
                    None if cg.first_come_sites is None else int(cg.first_come_sites),
                    cg.sites_total, cg.sites_not_bookable, cg.length_data_quality,
                    cg.provider, cg.id,
                ),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO campgrounds (
                     provider, id, name, rec_area, state, latitude, longitude,
                     reservation_type, status, status_reason, closed_until,
                     first_cataloged, last_checked, seeded, coord_source,
                     first_come_sites, sites_total, sites_not_bookable,
                     length_data_quality)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cg.provider, cg.id, cg.name, cg.rec_area, cg.state,
                    cg.latitude, cg.longitude, cg.reservation_type, cg.status,
                    cg.status_reason, cg.closed_until, stamp, stamp,
                    1 if seeded else 0, cg.coord_source,
                    None if cg.first_come_sites is None else int(cg.first_come_sites),
                    cg.sites_total, cg.sites_not_bookable, cg.length_data_quality,
                ),
            )
            added += 1
    conn.commit()
    return added, updated


def row_to_campground(row: sqlite3.Row) -> Campground:
    return Campground(
        provider=row["provider"],
        id=row["id"],
        name=row["name"],
        rec_area=row["rec_area"],
        state=row["state"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        reservation_type=row["reservation_type"] or "reservable",
        status=row["status"] or STATUS_UNKNOWN,
        status_reason=row["status_reason"],
        closed_until=row["closed_until"],
        coord_source=row["coord_source"] if "coord_source" in row.keys() else None,
        first_come_sites=(
            None if "first_come_sites" not in row.keys()
            or row["first_come_sites"] is None
            else bool(row["first_come_sites"])
        ),
        sites_total=row["sites_total"] if "sites_total" in row.keys() else None,
        sites_not_bookable=(
            row["sites_not_bookable"] if "sites_not_bookable" in row.keys() else None
        ),
        length_data_quality=(
            row["length_data_quality"] if "length_data_quality" in row.keys() else None
        ),
        **_optional_columns(row),
    )


#: Columns added late enough that a database may predate them. Read defensively
#: so an un-migrated file degrades to None rather than raising on every query.
_LATE_COLUMNS = ("water_nearby", "water_evidence", "photo_url", "photo_credit",
                 "description")


def _optional_columns(row: sqlite3.Row) -> dict:
    keys = row.keys()
    out = {name: (row[name] if name in keys else None) for name in _LATE_COLUMNS}
    raw = row["activities"] if "activities" in keys else None
    out["activities"] = loads(raw) if raw else None
    return out


def get_campground(
    conn: sqlite3.Connection, provider: str, cg_id: str
) -> Optional[Campground]:
    row = conn.execute(
        "SELECT * FROM campgrounds WHERE provider=? AND id=?", (provider, cg_id)
    ).fetchone()
    return row_to_campground(row) if row else None


def list_campgrounds(
    conn: sqlite3.Connection,
    states: Optional[Iterable[str]] = None,
    provider: Optional[str] = None,
) -> list[Campground]:
    sql = "SELECT * FROM campgrounds WHERE 1=1"
    params: list[Any] = []
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    states = list(states or [])
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY name"
    return [row_to_campground(r) for r in conn.execute(sql, params)]


def search_campgrounds(
    conn: sqlite3.Connection,
    q: str,
    states: Optional[Iterable[str]] = None,
) -> list[Campground]:
    """Free-text search over the CATALOG, not over search hits (§8k).

    This is why a full-but-catalogued park is still findable by name.
    """
    sql = "SELECT * FROM campgrounds WHERE (name LIKE ? OR rec_area LIKE ?)"
    like = f"%{q}%"
    params: list[Any] = [like, like]
    states = list(states or [])
    if states:
        sql += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    sql += " ORDER BY name"
    return [row_to_campground(r) for r in conn.execute(sql, params)]


def set_campground_status(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    status: str,
    reason: Optional[str] = None,
    closed_until: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    conn.execute(
        """UPDATE campgrounds
             SET status=?, status_reason=?, closed_until=COALESCE(?, closed_until),
                 last_checked=?
           WHERE provider=? AND id=?""",
        (status, reason, closed_until, iso(now), provider, cg_id),
    )
    conn.commit()


def set_campground_coordinates(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    latitude: Optional[float],
    longitude: Optional[float],
    source: str,
    now: Optional[datetime] = None,
) -> bool:
    """Record a coordinate and where it came from. Returns True if it landed.

    Two refusals, both deliberate:

    * **A missing coordinate never overwrites a present one.** A backfill that
      comes up empty must leave a good point alone.
    * **Provenance is required.** `source` is not optional, because a point
      from a province's open-data API and one parsed off a booking page are
      different claims and a later maintainer has to be able to tell them
      apart. Nothing here ever writes an estimate — a campground we cannot
      locate stays unlocated (§13).
    """
    if latitude is None or longitude is None:
        return False
    if not source:
        raise ValueError("a coordinate must record where it came from")
    cur = conn.execute(
        """UPDATE campgrounds
             SET latitude=?, longitude=?, coord_source=?, coord_updated=?
           WHERE provider=? AND id=?""",
        (latitude, longitude, source, iso(now), provider, cg_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_site_inventory(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    sites_total: int,
    sites_not_bookable: Optional[int],
    source: str,
    site_types: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Record a campground's site counts, measured once and kept.

    Inventory changes on the order of years, so this is written by a
    maintenance backfill and then left alone — it is never part of a scan.
    `first_come_sites` is derived here rather than stored independently, so the
    flag and the counts can never disagree.

    `sites_not_bookable` may be **None**, and that is not the same as 0. RIDB
    states `CampsiteReservable` per site; ReserveAmerica's park page does not —
    its last column reads "Enter Date", which is a prompt, not a flag. So for
    an RA park we know how many sites exist and nothing about how many are
    bookable, and `first_come_sites` stays NULL rather than being written as
    "none, definitely" (§8g: unknown is a third state, not a synonym for no).
    """
    if not source:
        raise ValueError("site counts must record where they came from")
    first_come = (
        None if sites_not_bookable is None else int(sites_not_bookable > 0)
    )
    cur = conn.execute(
        """UPDATE campgrounds
             SET sites_total=?, sites_not_bookable=?, first_come_sites=?,
                 site_types=?, inventory_source=?, inventory_updated=?
           WHERE provider=? AND id=?""",
        (sites_total, sites_not_bookable, first_come,
         dumps(site_types) if site_types else None,
         source, iso(now), provider, cg_id),
    )
    conn.commit()
    return cur.rowcount > 0


def upsert_campsites(
    conn: sqlite3.Connection,
    provider: str,
    campground_id: str,
    sites: Iterable[dict],
    source: str,
    now: Optional[datetime] = None,
) -> int:
    """Write a campground's per-site inventory. Measured once, then left alone."""
    stamp = iso(now)
    n = 0
    for site in sites:
        conn.execute(
            """INSERT INTO campsites (
                 provider, campground_id, site_id, name, loop, site_type,
                 type_of_use, reservable, max_vehicle_length, site_access,
                 access_class, driveway_entry, max_people, accessible,
                 latitude, longitude, permitted_equipment, attributes,
                 source, updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider, campground_id, site_id) DO UPDATE SET
                 name=excluded.name, loop=excluded.loop,
                 site_type=excluded.site_type, type_of_use=excluded.type_of_use,
                 reservable=excluded.reservable,
                 max_vehicle_length=excluded.max_vehicle_length,
                 site_access=excluded.site_access,
                 access_class=excluded.access_class,
                 driveway_entry=excluded.driveway_entry,
                 max_people=excluded.max_people, accessible=excluded.accessible,
                 latitude=excluded.latitude, longitude=excluded.longitude,
                 permitted_equipment=excluded.permitted_equipment,
                 attributes=excluded.attributes, source=excluded.source,
                 updated=excluded.updated""",
            (
                provider, campground_id, str(site["site_id"]), site.get("name"),
                site.get("loop"), site.get("site_type"), site.get("type_of_use"),
                None if site.get("reservable") is None else int(site["reservable"]),
                site.get("max_vehicle_length"), site.get("site_access"),
                site.get("access_class"), site.get("driveway_entry"),
                site.get("max_people"),
                None if site.get("accessible") is None else int(site["accessible"]),
                site.get("latitude"), site.get("longitude"),
                dumps(site.get("permitted_equipment")) if site.get("permitted_equipment") else None,
                dumps(site.get("attributes")) if site.get("attributes") else None,
                source, stamp,
            ),
        )
        n += 1
    conn.commit()
    return n


def set_facility_detail(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    activities: Optional[list] = None,
    photo_url: Optional[str] = None,
    photo_credit: Optional[str] = None,
    description: Optional[str] = None,
    water_nearby: Optional[str] = None,
    water_evidence: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Activities, a photo, a description, and the derived water verdict.

    All from one facility request (`inventory.fetch_facility_detail`). The
    water verdict is stored **beside** whatever the provider said, never over
    it: ReserveAmerica's own `Near Water` stays exactly as published, wrong
    and empty as it is, because overwriting a source's claim with our
    inference makes the two indistinguishable later.
    """
    cur = conn.execute(
        """UPDATE campgrounds
             SET activities=?, photo_url=?, photo_credit=?, description=?,
                 water_nearby=?, water_evidence=?
           WHERE provider=? AND id=?""",
        (dumps(activities) if activities else None, photo_url, photo_credit,
         description, water_nearby, water_evidence, provider, cg_id),
    )
    conn.commit()
    return cur.rowcount > 0


def set_water(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    water_nearby: Optional[str],
    water_evidence: Optional[str],
    now: Optional[datetime] = None,
) -> bool:
    """Just the water verdict, without disturbing the photo or description.

    Separate from `set_facility_detail` because re-deriving must never blank
    fields it wasn't asked about — the re-derive runs offline and knows
    nothing about photos.
    """
    cur = conn.execute(
        "UPDATE campgrounds SET water_nearby=?, water_evidence=? "
        "WHERE provider=? AND id=?",
        (water_nearby, water_evidence, provider, cg_id),
    )
    conn.commit()
    return cur.rowcount > 0


def has_campsites(conn: sqlite3.Connection, provider: str, campground_id: str) -> bool:
    """Do we already hold this campground's per-site inventory?"""
    row = conn.execute(
        "SELECT 1 FROM campsites WHERE provider=? AND campground_id=? LIMIT 1",
        (provider, campground_id),
    ).fetchone()
    return row is not None


def list_campsites(
    conn: sqlite3.Connection,
    provider: str,
    campground_id: str,
) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM campsites WHERE provider=? AND campground_id=? ORDER BY name",
        (provider, campground_id),
    )
    return [dict(r) for r in rows]


def set_length_quality(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    quality: str,
    now: Optional[datetime] = None,
) -> None:
    """How far this campground's driveway figures can be trusted."""
    conn.execute(
        "UPDATE campgrounds SET length_data_quality=? WHERE provider=? AND id=?",
        (quality, provider, cg_id),
    )
    conn.commit()


# Providers whose catalogue coordinate locates the PARK, not the campground
# inside it. Verified on the map 2026-07-29: Cape Lookout State Park's pin sits
# about a kilometre south of its campground, near the cape trailhead, and Cape
# Disappointment's sits in the middle of the park with the campground well to
# the south. Both are the coordinate the provider itself publishes — CampSage
# draws Cape Disappointment on the identical spot — so this is the limit of the
# data, not a bug to fix. What is ours to fix is saying so.
#
# RecreationDotGov is deliberately absent: RIDB gives one facility record per
# campground, so its coordinate is the campground.
_PARK_LEVEL_PROVIDER_PREFIXES = ("ReserveAmerica", "GoingToCamp")


def coordinate_precision(provider: str, coord_source: Optional[str] = None) -> Optional[str]:
    """What a pin actually locates: 'campground', 'park', or None for unknown.

    Derived from the provider rather than stored per row, because the committed
    seed predates this distinction and re-enumerating 803 campgrounds to write
    one string into each would mean a live walk of every provider (§13). A
    `coord_source` that names a park-level lookup still wins where present.
    """
    if coord_source and "park" in coord_source.lower():
        return "park"
    if provider.split(":", 1)[0] in _PARK_LEVEL_PROVIDER_PREFIXES:
        return "park"
    return "campground"


def coordinate_provenance(conn: sqlite3.Connection) -> dict[str, int]:
    """How many catalogue coordinates came from where — including unlocated."""
    counts: dict[str, int] = {}
    for row in conn.execute(
        """SELECT COALESCE(coord_source, CASE WHEN latitude IS NULL
                   THEN 'unlocated' ELSE 'provider enumeration' END) AS src,
                  COUNT(*) AS n
             FROM campgrounds GROUP BY src"""
    ):
        counts[row["src"]] = row["n"]
    return counts


def stamp_status_from_availability(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    checked_ok: bool,
    now: Optional[datetime] = None,
) -> str:
    """Derive a catalog status from what the scan just found (§8 cycle step 1).

    A failed live check downgrades to `stale` — it never removes the pin.
    """
    if not checked_ok:
        set_campground_status(
            conn, provider, cg_id, STATUS_STALE, "live check failed", now=now
        )
        return STATUS_STALE
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM availability WHERE provider=? AND facility_id=?",
        (provider, cg_id),
    ).fetchone()["n"]
    if count:
        set_campground_status(conn, provider, cg_id, STATUS_AVAILABLE, None, now=now)
        return STATUS_AVAILABLE

    # Nothing found. What that MEANS depends on whether the campground can be
    # reserved at all (§4). For a first-come site there is no reservation feed,
    # so an empty result says nothing about whether sites are free — it is
    # exactly as uninformative as not looking. Calling it "full" would send
    # someone driving past a campground with space, which is the Reehers
    # failure inverted: the map asserting something it does not know.
    row = conn.execute(
        "SELECT reservation_type FROM campgrounds WHERE provider=? AND id=?",
        (provider, cg_id),
    ).fetchone()
    if row and (row["reservation_type"] or "reservable") == "first_come":
        set_campground_status(
            conn, provider, cg_id, STATUS_UNKNOWN,
            "first-come, first-served — availability can't be checked online",
            now=now,
        )
        return STATUS_UNKNOWN

    set_campground_status(
        conn, provider, cg_id, STATUS_FULL,
        "no open sites in the scanned window", now=now,
    )
    return STATUS_FULL


def map_view(
    conn: sqlite3.Connection,
    states: Optional[Iterable[str]] = None,
) -> list[dict]:
    """What the map draws: every catalogued campground + its status (§8k).

    Full / unknown / stale campgrounds are included by design.
    """
    out = []
    for cg in list_campgrounds(conn, states=states):
        open_sites = conn.execute(
            "SELECT COUNT(*) AS n FROM availability WHERE provider=? AND facility_id=?",
            (cg.provider, cg.id),
        ).fetchone()["n"]
        out.append(
            {
                "provider": cg.provider,
                "id": cg.id,
                "name": cg.name,
                "state": cg.state,
                "latitude": cg.latitude,
                "longitude": cg.longitude,
                "status": cg.status,
                "status_reason": cg.status_reason,
                "open_sites": open_sites,
                "located": cg.has_location,
                "reservation_type": cg.reservation_type,
                # Three-state and deliberately nullable: the UI must be able to
                # say nothing about first-come sites when we don't know (§8g).
                "first_come_sites": cg.first_come_sites,
                "sites_total": cg.sites_total,
                "sites_not_bookable": cg.sites_not_bookable,
                "booking_label": cg.booking_label,
                "length_data_quality": cg.length_data_quality,
                # What the pin locates. A park-level coordinate can be a mile
                # from the sites, and a map that doesn't say so is quietly
                # precise about something it doesn't know.
                "coord_precision": (
                    coordinate_precision(cg.provider, cg.coord_source)
                    if cg.has_location else None
                ),
            }
        )
    return out


# --------------------------------------------------------------------------
# watches (§8b)
# --------------------------------------------------------------------------

@dataclass
class Watch:
    id: Optional[int] = None
    user_id: Optional[int] = None
    name: str = ""
    provider: Optional[str] = None
    mode: str = "targeted"                 # 'targeted' | 'autonomous'
    rec_area_ids: list[str] = field(default_factory=list)
    campground_ids: list[str] = field(default_factory=list)
    campsite_ids: list[str] = field(default_factory=list)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    nights: int = 1
    weekends_only: bool = False
    filters: dict = field(default_factory=dict)
    notify_targets: list[str] = field(default_factory=list)
    active: bool = True


def add_watch(conn: sqlite3.Connection, watch: Watch, now: Optional[datetime] = None) -> int:
    cur = conn.execute(
        """INSERT INTO watches (
             user_id, name, provider, mode, rec_area_ids, campground_ids, campsite_ids,
             start_date, end_date, nights, weekends_only, filters, notify_targets,
             active, created)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            watch.user_id, watch.name, watch.provider, watch.mode,
            dumps(watch.rec_area_ids), dumps(watch.campground_ids), dumps(watch.campsite_ids),
            str(watch.start_date) if watch.start_date else None,
            str(watch.end_date) if watch.end_date else None,
            watch.nights, 1 if watch.weekends_only else 0,
            dumps(watch.filters), dumps(watch.notify_targets),
            1 if watch.active else 0, iso(now),
        ),
    )
    conn.commit()
    watch.id = cur.lastrowid
    return cur.lastrowid


def watch_from_config(entry: dict) -> Watch:
    """Build a Watch from a `config.yaml` `watches:` entry (§8c)."""
    filters = dict(entry.get("filters") or {})
    # The config example puts `states` at the top level of a watch; the
    # matcher reads it out of `filters`, so fold it in.
    if entry.get("states"):
        filters.setdefault("states", [str(s) for s in entry["states"]])
    return Watch(
        name=entry.get("name", "unnamed"),
        provider=entry.get("provider"),
        mode=entry.get("mode", "targeted"),
        rec_area_ids=[str(v) for v in (entry.get("rec_area_ids") or [])],
        campground_ids=[str(v) for v in (entry.get("campground_ids") or [])],
        campsite_ids=[str(v) for v in (entry.get("campsite_ids") or [])],
        start_date=to_date(entry.get("start_date")),
        end_date=to_date(entry.get("end_date")),
        nights=int(entry.get("nights", 1)),
        weekends_only=bool(entry.get("weekends_only")),
        filters=filters,
        notify_targets=list(entry.get("notify_targets") or []),
        active=bool(entry.get("active", True)),
    )


def seed_watches(
    conn: sqlite3.Connection,
    entries: Iterable[dict],
    now: Optional[datetime] = None,
) -> int:
    """Insert config-declared watches that don't exist yet. Returns count added.

    §8c calls these "optional seeds" — so this is insert-if-absent, matched by
    name. It never updates or deletes, so editing a watch in the app is not
    silently reverted by the config file on the next run.
    """
    added = 0
    for entry in entries:
        watch = watch_from_config(entry)
        exists = conn.execute(
            "SELECT 1 FROM watches WHERE name = ?", (watch.name,)
        ).fetchone()
        if exists:
            continue
        add_watch(conn, watch, now=now)
        added += 1
    return added


def row_to_watch(row: sqlite3.Row) -> Watch:
    return Watch(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        provider=row["provider"],
        mode=row["mode"] or "targeted",
        rec_area_ids=loads(row["rec_area_ids"], []),
        campground_ids=loads(row["campground_ids"], []),
        campsite_ids=loads(row["campsite_ids"], []),
        start_date=to_date(row["start_date"]),
        end_date=to_date(row["end_date"]),
        nights=row["nights"] or 1,
        weekends_only=bool(row["weekends_only"]),
        filters=loads(row["filters"], {}),
        notify_targets=loads(row["notify_targets"], []),
        active=bool(row["active"]),
    )


def list_watches(conn: sqlite3.Connection, active_only: bool = True) -> list[Watch]:
    sql = "SELECT * FROM watches"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY id"
    return [row_to_watch(r) for r in conn.execute(sql)]


def set_watch_active(conn: sqlite3.Connection, watch_id: int, active: bool) -> None:
    conn.execute("UPDATE watches SET active=? WHERE id=?", (1 if active else 0, watch_id))
    conn.commit()


def watch_matches(watch: Watch, sites: Iterable[Campsite], home_base=None) -> list[Campsite]:
    """Which of these campsites satisfy the watch's scope + constraints.

    Enrichment gates (AQI, fire) are deliberately NOT applied here — those are
    step 4's three-state engine (§8g). This is scope + date + distance only.
    """
    out = []
    for site in sites:
        if watch.provider and site.provider != watch.provider:
            continue
        if watch.campsite_ids and site.campsite_id not in watch.campsite_ids:
            continue
        if watch.campground_ids and site.facility_id not in watch.campground_ids:
            continue
        if watch.rec_area_ids and site.rec_area_id not in watch.rec_area_ids:
            continue
        if watch.start_date and site.available_date < watch.start_date:
            continue
        if watch.end_date:
            last_night = site.available_date + timedelta(days=site.nights - 1)
            if last_night > watch.end_date:
                continue
        if watch.nights and site.nights < watch.nights:
            continue
        if watch.weekends_only and not is_weekend_night(site.available_date):
            continue

        states = watch.filters.get("states") or []
        if states and site.state not in states:
            continue

        types = watch.filters.get("campsite_type_any") or []
        if types and (site.campsite_type or "") not in types:
            continue

        max_miles = watch.filters.get("max_miles")
        if max_miles is not None and home_base:
            miles = haversine_miles(
                home_base[0], home_base[1], site.latitude, site.longitude
            )
            # miles is None => unlocated. Excluded from distance filtering rather
            # than silently failed (§13) — but it also can't satisfy a distance
            # rule, so a distance-constrained watch skips it.
            if miles is None or miles > max_miles:
                continue

        out.append(site)
    return out


# --------------------------------------------------------------------------
# notifications (§8b)
# --------------------------------------------------------------------------

def already_notified(
    conn: sqlite3.Connection,
    watch_id: int,
    campsite_key: str,
    cooldown_hours: int = DEFAULT_RENOTIFY_COOLDOWN_HOURS,
    now: Optional[datetime] = None,
) -> bool:
    row = conn.execute(
        """SELECT sent_at FROM notifications
            WHERE watch_id=? AND campsite_key=?
            ORDER BY sent_at DESC LIMIT 1""",
        (watch_id, campsite_key),
    ).fetchone()
    if not row:
        return False
    sent_at = parse_iso(row["sent_at"])
    if sent_at is None:
        return False
    return (now or utcnow()) - sent_at < timedelta(hours=cooldown_hours)


def record_notification(
    conn: sqlite3.Connection,
    watch_id: int,
    campsite_key: str,
    now: Optional[datetime] = None,
) -> None:
    conn.execute(
        "INSERT INTO notifications (watch_id, campsite_key, sent_at) VALUES (?,?,?)",
        (watch_id, campsite_key, iso(now)),
    )
    conn.commit()


def pending_notifications(
    conn: sqlite3.Connection,
    watch: Watch,
    matches: Iterable[Campsite],
    cooldown_hours: int = DEFAULT_RENOTIFY_COOLDOWN_HOURS,
    now: Optional[datetime] = None,
) -> list[Campsite]:
    """Matches that haven't been alerted on within the cooldown."""
    return [
        s for s in matches
        if not already_notified(conn, watch.id, s.key, cooldown_hours, now)
    ]


# --------------------------------------------------------------------------
# scanner status (docs/scanning-design.md — "Telling the user what's happening")
# --------------------------------------------------------------------------

SCAN_IDLE = "idle"
SCAN_SCANNING = "scanning"
SCAN_WAITING = "waiting"       # deliberately spaced out, not stuck
SCAN_BLOCKED = "blocked"       # a host told us to stop; we are honouring it


@dataclass
class ScanStatus:
    """One honest sentence about what the scanner is doing, plus the numbers.

    `message` is written for a person, not a log: "Checking 8 campgrounds — 3
    done". `detail` carries the reason for a wait, which is the part that turns
    unexplained slowness into explained slowness.
    """

    state: str = SCAN_IDLE
    provider: Optional[str] = None
    target: Optional[str] = None
    done: int = 0
    total: int = 0
    message: str = ""
    detail: Optional[str] = None
    started: Optional[str] = None
    updated: Optional[str] = None

    @property
    def note(self) -> str:
        """The standing explanation for the pace. Never varies, never hidden."""
        return PACING_NOTE

    @property
    def busy(self) -> bool:
        return self.state in (SCAN_SCANNING, SCAN_WAITING)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "provider": self.provider,
            "target": self.target,
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "detail": self.detail,
            "note": self.note,
            "started": self.started,
            "updated": self.updated,
        }


def set_scan_status(
    conn: sqlite3.Connection,
    status: ScanStatus,
    now: Optional[datetime] = None,
) -> ScanStatus:
    """Write the single status row. Cheap enough to call before every request."""
    status.updated = iso(now)
    conn.execute(
        """INSERT INTO scan_status (
             id, state, provider, target, done, total, message, detail, started, updated)
           VALUES (1,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             state=excluded.state, provider=excluded.provider, target=excluded.target,
             done=excluded.done, total=excluded.total, message=excluded.message,
             detail=excluded.detail, started=excluded.started, updated=excluded.updated""",
        (
            status.state, status.provider, status.target, status.done, status.total,
            status.message, status.detail, status.started, status.updated,
        ),
    )
    conn.commit()
    return status


def get_scan_status(conn: sqlite3.Connection) -> ScanStatus:
    """The scanner's current state. Never absent — an unwritten row reads idle."""
    row = conn.execute("SELECT * FROM scan_status WHERE id = 1").fetchone()
    if not row:
        return ScanStatus()
    return ScanStatus(
        state=row["state"] or SCAN_IDLE,
        provider=row["provider"],
        target=row["target"],
        done=row["done"] or 0,
        total=row["total"] or 0,
        message=row["message"] or "",
        detail=row["detail"],
        started=row["started"],
        updated=row["updated"],
    )
