"""Site-inventory backfill — how many sites at a campground aren't bookable.

A maintenance job, like `coordinates.py`, and deliberately outside the scan
cycle: a campground's inventory changes on the order of years.

    python3 scripts/manage.py backfill-site-inventory --state OR

## What this can and cannot establish

Researched live 2026-07-28 (`docs/first-come-research.md`). RIDB's
`facilities/{id}/campsites` returns a record per site carrying
`CampsiteReservable`, `CampsiteType`, and `TypeOfUse`. From that we can count
how many sites exist and how many are **not bookable online**.

What we cannot establish is that those sites are first-come. RIDB never says
"first come first served" anywhere. A non-reservable site may be first-come, a
seasonal closure, or one simply never loaded into the booking system — and
some are `MANAGEMENT`, which are camp-host and staff pitches that no camper
may use. Those are excluded here always; counting them would offer somebody a
site that is somebody else's job.

So the recorded fact is deliberately narrow: **"not bookable online"**. The
interface must not upgrade that to "first-come and probably free".

## The campgrounds this cannot help

Every facility RIDB flags non-reservable returns **zero** campsite records —
its inventory comes from the reservation system, so a campground that takes no
bookings was never loaded into it. That is 206 of our 803, and they are exactly
the ones where a site count would be most useful. They stay `unknown` rather
than being recorded as zero, because "RIDB doesn't know" and "there are no
sites" are different statements.

## Two silent-wrong-answer traps, both guarded below

* `campsites?facilityID=X` as a **query parameter is ignored** — it returns all
  137,117 campsites in RIDB. Only the nested `facilities/{id}/campsites` path
  filters.
* Paging is checked against the API's own `TOTAL_COUNT`; a short read raises
  rather than silently under-counting a campground's sites.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from . import equipment, store
from .pacing import shared_limiter

log = logging.getLogger(__name__)

RIDB_HOST = "ridb.recreation.gov"
RIDB_SOURCE = "RIDB facilities/{id}/campsites"
PAGE_SIZE = 50

#: Camp-host and staff pitches. Never available to the public, so never counted
#: as something a camper might get.
EXCLUDED_TYPES = frozenset({"MANAGEMENT"})


class IncompleteInventory(RuntimeError):
    """A facility's site list came back short, so we record nothing for it."""


@dataclass
class InventoryReport:
    visited: int = 0
    recorded: int = 0
    no_inventory: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    mixed: int = 0

    def summary(self) -> str:
        return (
            f"visited={self.visited} recorded={self.recorded} "
            f"mixed={self.mixed} no-inventory={len(self.no_inventory)} "
            f"errors={len(self.errors)}"
        )


@dataclass
class SiteCounts:
    total: int
    bookable: int
    not_bookable: int          # excludes MANAGEMENT
    management: int
    #: `CampsiteType` -> {"bookable": n, "not_bookable": n}. Kept because it
    #: answers a different question from this module's: **how you reach a site**
    #: (hike-in, boat-in, equestrian, tent-only) rather than how you book it.
    #: Captured from the same requests so that question never costs a second
    #: pass over every facility.
    types: dict = field(default_factory=dict)

    @property
    def has_unbookable_sites(self) -> bool:
        return self.not_bookable > 0


#: The source spells the access mode several ways — "Drive-In" 2711 times,
#: "Drive In" 36, "Hike-In" 152, "Hike In" 2, "Walk-In" 11. A filter matching
#: exact strings would silently miss real hike-in sites, which is the same
#: shortfall shape as every other bug found today. Normalize on the way in and
#: keep the raw value beside it.
#:
#: Walk-in and hike-in collapse to one class deliberately: both mean "park
#: elsewhere and carry your gear", the axis Scott defined (docs/terminology.md).
ACCESS_HIKE_IN = "hike_in"
ACCESS_DRIVE_IN = "drive_in"


def normalize_access(value: Optional[str]) -> Optional[str]:
    """Canonical access class, or None when the source didn't say."""
    text = (value or "").strip().lower().replace("-", " ")
    if not text or text in ("n/a", "na"):
        return None
    if "hike" in text or "walk" in text:
        return ACCESS_HIKE_IN
    if "drive" in text:
        return ACCESS_DRIVE_IN
    return None


def parse_ridb_site(record: dict) -> dict:
    """One RIDB campsite record, normalized.

    Keeps what the earlier version threw away. RIDB states, per site:
    `Max Vehicle Length`, `Site Access` (Drive-In / Hike-In — the access axis,
    explicitly), `Driveway Entry`, `Loop`, per-site coordinates, and permitted
    equipment. Running 545 facilities' worth of requests and storing only a
    count would have discarded all of it.
    """
    attributes = {
        a.get("AttributeName"): a.get("AttributeValue")
        for a in record.get("ATTRIBUTES") or []
    }

    def number(name):
        raw = attributes.get(name)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        # Upstream writes 0 for "not applicable", not "zero feet".
        return value or None

    def coord(value):
        try:
            f = float(value)
        except (TypeError, ValueError):
            return None
        return f or None

    return {
        "site_id": record.get("CampsiteID"),
        "name": record.get("CampsiteName"),
        "loop": record.get("Loop") or None,
        "site_type": (record.get("CampsiteType") or "").strip().upper() or None,
        "type_of_use": record.get("TypeOfUse"),
        "reservable": bool(record.get("CampsiteReservable")),
        "max_vehicle_length": number("Max Vehicle Length"),
        "site_access": attributes.get("Site Access"),
        "access_class": normalize_access(attributes.get("Site Access")),
        "driveway_entry": attributes.get("Driveway Entry"),
        "max_people": number("Max Num of People"),
        "accessible": bool(record.get("CampsiteAccessible")),
        "latitude": coord(record.get("CampsiteLatitude")),
        "longitude": coord(record.get("CampsiteLongitude")),
        "permitted_equipment": record.get("PERMITTEDEQUIPMENT") or None,
        "attributes": attributes or None,
    }


def classify_sites(records: Iterable[dict]) -> SiteCounts:
    """Count a facility's sites. `MANAGEMENT` is excluded from both sides."""
    total = bookable = not_bookable = management = 0
    types: dict = {}
    for record in records:
        site_type = (record.get("CampsiteType") or "UNKNOWN").strip().upper()
        if site_type in EXCLUDED_TYPES:
            management += 1
            continue
        total += 1
        bucket = types.setdefault(site_type, {"bookable": 0, "not_bookable": 0})
        if record.get("CampsiteReservable"):
            bookable += 1
            bucket["bookable"] += 1
        else:
            not_bookable += 1
            bucket["not_bookable"] += 1
    return SiteCounts(total, bookable, not_bookable, management, types)


def fetch_facility_campsites(facility_id: str, fetcher=None) -> list[dict]:
    """Every campsite record for one facility, paged and count-checked."""
    fetch = fetcher or _ridb_get
    records: list[dict] = []
    offset, total = 0, None
    while True:
        payload = fetch(
            # The NESTED path. `campsites?facilityID=` is silently ignored and
            # returns every campsite in RIDB.
            f"facilities/{facility_id}/campsites",
            {"limit": PAGE_SIZE, "offset": offset},
        )
        batch = payload.get("RECDATA") or []
        records.extend(batch)
        if total is None:
            total = (
                payload.get("METADATA", {}).get("RESULTS", {}).get("TOTAL_COUNT")
            )
        offset += PAGE_SIZE
        if not batch or total is None or offset >= total:
            break

    if total is not None and len(records) < total:
        raise IncompleteInventory(
            f"facility {facility_id}: RIDB reported {total} campsites but "
            f"{len(records)} arrived — refusing to record a short count."
        )
    return records


def _ridb_get(path: str, params: dict) -> dict:
    from .providers.camply_provider import CamplyProvider

    provider = CamplyProvider("RecreationDotGov")
    client = provider._search_class().provider_class()
    with shared_limiter().slot(RIDB_HOST, label="RIDB campsites"):
        return client.get_ridb_data(path, params)


def backfill_site_inventory(
    conn: sqlite3.Connection,
    provider: str = "RecreationDotGov",
    states: Optional[list[str]] = None,
    limit: Optional[int] = None,
    fetcher=None,
    report: Optional[InventoryReport] = None,
    now: Optional[datetime] = None,
) -> InventoryReport:
    """Record each campground's bookable/not-bookable site counts.

    Only fills blanks — a campground already carrying an answer is skipped, so
    re-running is cheap and cannot churn. Nothing is written for a facility
    RIDB has no inventory for; unknown stays unknown.
    """
    report = report or InventoryReport()
    # "Already done" means we hold the PER-SITE rows, not merely a count. An
    # earlier version of this backfill stored counts only, and those campgrounds
    # were committed to the seed — so testing the old flag would skip exactly
    # the ones that still need the richer pass.
    campgrounds = [
        cg for cg in store.list_campgrounds(conn, provider=provider, states=states)
        if not store.has_campsites(conn, cg.provider, cg.id)
    ]
    if limit:
        campgrounds = campgrounds[:limit]

    for campground in campgrounds:
        report.visited += 1
        try:
            records = fetch_facility_campsites(campground.id, fetcher=fetcher)
        except Exception as exc:  # noqa: BLE001 - one bad facility is not fatal
            log.warning("inventory failed for %s: %s", campground.name, exc)
            report.errors[campground.name] = str(exc)
            continue

        if not records:
            # RIDB has no site list for this facility. That is not "zero
            # sites" — it is "we don't know", and it must not be recorded as
            # an answer. True of every first-come facility.
            report.no_inventory.append(campground.name)
            continue

        counts = classify_sites(records)
        if counts.total == 0:
            report.no_inventory.append(campground.name)
            continue

        parsed = [parse_ridb_site(r) for r in records
                  if (r.get("CampsiteType") or "").strip().upper() not in EXCLUDED_TYPES]
        store.upsert_campsites(
            conn, campground.provider, campground.id, parsed,
            source=RIDB_SOURCE, now=now,
        )
        store.set_site_inventory(
            conn, campground.provider, campground.id,
            sites_total=counts.total,
            sites_not_bookable=counts.not_bookable,
            site_types=counts.types,
            source=RIDB_SOURCE, now=now,
        )
        # How far THIS campground's driveway figures can be trusted — measured
        # from its own spread, never assumed from who runs it.
        store.set_length_quality(
            conn, campground.provider, campground.id,
            equipment.grade_lengths(s["max_vehicle_length"] for s in parsed),
            now=now,
        )
        report.recorded += 1
        if counts.has_unbookable_sites:
            report.mixed += 1
    log.info("%s inventory: %s", provider, report.summary())
    return report
