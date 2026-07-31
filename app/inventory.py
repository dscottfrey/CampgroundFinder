"""Site-inventory backfill — how many sites at a campground aren't bookable.

A maintenance job, like `coordinates.py`, and deliberately outside the scan
cycle: a campground's inventory changes on the order of years.

    python3 scripts/manage.py backfill-site-inventory --state OR
    python3 scripts/manage.py backfill-site-inventory --provider ReserveAmerica:OR

## Two sources, and they know different things

**RIDB** (`facilities/{id}/campsites`) states `CampsiteReservable` per site, so
for a federal campground we can count what is not bookable online.

**ReserveAmerica** (the park's own site table) states none of that — its last
column reads "Enter Date", a prompt rather than a flag. What it *does* state,
per site and better than RIDB, is the access mode (`WALK TO`) and a driveway
cell whose emptiness is meaningful. So an RA park records `sites_total` and
leaves `sites_not_bookable` **None**: not measured, which is not zero.

That asymmetry is the point of keeping the two paths separate rather than
flattening them into a common shape that has to lie about one of them.

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
RA_SOURCE = "ReserveAmerica campgroundDetails.do"
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
    #: None where the source states no per-site bookability. ReserveAmerica's
    #: park page is one: it lists every site and never says which can be booked
    #: online. None means "not measured"; 0 would mean "measured, and none".
    bookable: Optional[int]
    not_bookable: Optional[int]   # excludes host / MANAGEMENT pitches
    management: int
    #: `CampsiteType` -> {"bookable": n, "not_bookable": n}. Kept because it
    #: answers a different question from this module's: **how you reach a site**
    #: (hike-in, boat-in, equestrian, tent-only) rather than how you book it.
    #: Captured from the same requests so that question never costs a second
    #: pass over every facility.
    types: dict = field(default_factory=dict)

    @property
    def has_unbookable_sites(self) -> bool:
        return bool(self.not_bookable)


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


def is_host_site(record: dict) -> bool:
    """Is this a camp-host pitch rather than something a camper may book?

    RIDB types these `MANAGEMENT`. ReserveAmerica has no such type and gives it
    away two different ways (docs/terminology.md), **both of which have to be
    checked**: sometimes the Site type reads `HOST SITE`, and sometimes the
    type is ordinary and only the site *name* says `Host` — Reehers' A-08 is
    typed `HORSE SITE` and named `Host`, which a type-only test walks straight
    past. Offering someone a host pitch is offering them somebody's job.
    """
    site_type = (record.get("site_type") or "").strip().upper()
    name = (record.get("name") or "").strip().upper()
    if "HOST" in site_type:
        return True
    # Anchored, not a substring test: "HOSTEL" would match `in`, and a site
    # legitimately named "Hostetler Point" should not vanish from a park.
    return name == "HOST" or name.startswith("HOST ") or name.endswith(" HOST")


def parse_ra_amenities(amenities: Iterable[str]) -> dict:
    """The Amenities icon captions, as {feature: value}.

    ReserveAmerica states **absence explicitly** — "Electric Hookup - no" sits
    beside "Electric Hookup available: 30 amp" — so a contains-check for
    "hookup" marks every unserviced site as serviced (docs/terminology.md).
    Split the caption instead and keep the answer, including the "no".

    Four caption shapes are in the live data, and the fourth was missed on the
    first pass (found 2026-07-31 by surveying all 64 Oregon parks):

    | caption                          | meaning              |
    |----------------------------------|----------------------|
    | `Full Hookup - no`               | absent               |
    | `Water Hookup`                   | present              |
    | `Electric Hookup available: 30 amp` | present, with a value |
    | `Full Hookup available`          | present — **no colon** |

    Left alone, that last one becomes a *separate feature* called "Full Hookup
    available", so the same hookup answers `no` 2,049 times under one key and
    `yes` 1,275 times under another, and any filter reading either key gets
    half the truth. The trailing " available" is stripped from the name.
    """
    parsed: dict[str, str] = {}
    for caption in amenities or ():
        text = (caption or "").strip()
        if not text:
            continue
        if " available:" in text:
            feature, _, value = text.partition(" available:")
            value = value.strip() or "yes"
        elif text.endswith(" - no"):
            feature, value = text[: -len(" - no")], "no"
        else:
            feature, value = text, "yes"
        feature = feature.strip()
        # "Full Hookup available" and "Full Hookup" are one feature.
        if feature.endswith(" available"):
            feature = feature[: -len(" available")].strip()
        parsed[feature] = value
    return parsed


def parse_ra_site(record: dict) -> dict:
    """One ReserveAmerica site-table row, normalized to the campsites schema.

    Three fields need care, all of them settled against the live pages:

    * **Site type is the access mode**, and it is authoritative — Brooke
      Creek's sites read `WALK TO`. It is *not* an equipment restriction:
      Beverly Beach C27 reads `TENT SITE` and Scott has camped it in a van.
      So it feeds `site_access` / `access_class`, never a length bound.
    * **The driveway cell's presence is a signal; its number is not.** Blank
      means no vehicle reaches the site. A number is a floor entered on a
      setup form, often a default — Beverly Beach A01 reads `20 Back-In` and
      is really 53 ft. Stored as-is so `equipment.grade_lengths` can judge
      each park by its own spread. Reehers' rows read a bare `Back-In` with
      no figure at all: a driveway exists, its length is unknown.
    * **`reservable` stays None.** The park page has no such column — the last
      cell reads "Enter Date". Writing False there would invent a fact.

    The type icon is carried into `attributes` under a name that says not to
    trust it: those `WALK TO` sites are iconed `rv`.
    """
    from .providers.reserveamerica import parse_driveway

    feet, manoeuvre = parse_driveway(record.get("equipment_length"))
    site_type = (record.get("site_type") or "").strip().upper() or None
    amenities = parse_ra_amenities(record.get("amenities") or [])

    def number(value):
        try:
            return int(str(value).strip()) or None
        except (TypeError, ValueError):
            return None

    return {
        "site_id": record.get("site_id"),
        "name": record.get("name"),
        "loop": record.get("loop") or None,
        "site_type": site_type,
        # RA's table doesn't carry RIDB's overnight/day-use distinction.
        "type_of_use": None,
        "reservable": None,
        "max_vehicle_length": feet,
        "site_access": site_type,
        "access_class": normalize_access(site_type),
        "driveway_entry": manoeuvre,
        "max_people": number(record.get("max_people")),
        # RA lists "Accessible site" when a site has it and says nothing when
        # it doesn't — unlike the hookups, absence is not stated. So absence
        # is unknown, not False.
        "accessible": True if "Accessible site" in amenities else None,
        # No per-site coordinates on this platform.
        "latitude": None,
        "longitude": None,
        "permitted_equipment": None,
        "attributes": {
            **amenities,
            # Deliberately NOT called site_type or access: ground-truthed as
            # wrong at Brooke Creek. Kept only so we can re-examine it.
            "untrusted_type_icon": record.get("type_icon"),
        } or None,
    }


def classify_ra_sites(records: Iterable[dict]) -> SiteCounts:
    """Count an RA park's sites. Host pitches are excluded, as MANAGEMENT is.

    `not_bookable` comes back **None, not 0** — the park page states no
    per-site bookability at all, and recording 0 would assert every site is
    bookable online, which we have not measured.
    """
    total = management = 0
    types: dict = {}
    for record in records:
        if is_host_site(record):
            management += 1
            continue
        total += 1
        site_type = (record.get("site_type") or "UNKNOWN").strip().upper()
        bucket = types.setdefault(
            site_type, {"bookable": 0, "not_bookable": 0, "unknown": 0})
        bucket["unknown"] += 1
    return SiteCounts(total, bookable=None, not_bookable=None,
                      management=management, types=types)


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


@dataclass
class _Source:
    """How to get one campground's site list out of a particular provider."""
    #: campground -> list of raw per-site records
    fetch: object
    #: one raw record -> the campsites-table shape
    parse: object
    #: all raw records -> SiteCounts
    classify: object
    #: raw record -> True when it must never be offered to a camper. Both
    #: sources have such rows and spell them differently: RIDB types them
    #: MANAGEMENT, RA names or types them Host.
    exclude: object
    #: recorded in `campsites.source`, so a row can always name its origin
    label: str


def _is_ridb_management(record: dict) -> bool:
    return (record.get("CampsiteType") or "").strip().upper() in EXCLUDED_TYPES


def _ridb_source(fetcher=None) -> _Source:
    return _Source(
        fetch=lambda cg: fetch_facility_campsites(cg.id, fetcher=fetcher),
        parse=parse_ridb_site,
        classify=classify_sites,
        exclude=_is_ridb_management,
        label=RIDB_SOURCE,
    )


def _reserveamerica_source(provider: str, fetcher=None) -> _Source:
    """ReserveAmerica: the park's own site table, walked to the end.

    `list_sites` already does the hard part — the park page shows 25 rows and
    pages through a *separate* endpoint, and the walk is checked against the
    park's own "N site(s) found" so a short read raises instead of quietly
    recording a 279-site park as a 27-site one.

    Pacing is the provider's own shared limiter (6s between requests to this
    host), so a large park is slow by design rather than by accident.
    """
    if fetcher is None:
        from .providers import build_provider

        client = build_provider(provider)
        fetcher = lambda cg: client.list_sites(cg.id)  # noqa: E731
    return _Source(
        fetch=fetcher,
        parse=parse_ra_site,
        classify=classify_ra_sites,
        exclude=is_host_site,
        label=RA_SOURCE,
    )


def _source_for(provider: str, fetcher=None) -> _Source:
    family = provider.partition(":")[0]
    if family == "ReserveAmerica":
        return _reserveamerica_source(provider, fetcher=fetcher)
    if family == "RecreationDotGov":
        return _ridb_source(fetcher=fetcher)
    raise ValueError(
        f"no site-inventory source for provider {provider!r} — "
        f"RecreationDotGov and ReserveAmerica:* are implemented"
    )


def fetch_facility_detail(facility_id: str, fetcher=None) -> dict:
    """One facility's full RIDB record — activities, media, description.

    `full=true` returns all three in a single request, which is why the water
    question, the campground photo and the operator's own description are
    settled together rather than in three passes over 545 facilities.
    """
    fetch = fetcher or _ridb_get
    payload = fetch(f"facilities/{facility_id}", {"full": "true"})
    record = payload.get("RECDATA", payload)
    if isinstance(record, list):
        record = record[0] if record else {}
    return record or {}


def parse_facility_detail(record: dict) -> dict:
    """Activities, a photo and a description, normalized.

    The photo is the record's **preview** image where it flags one, else its
    first — RIDB's MEDIA list mixes campground scenery with maps and logos,
    and `IsPreview` is the operator's own pick of the useful one.
    """
    activities = sorted({
        (a.get("ActivityName") or "").strip().upper()
        for a in record.get("ACTIVITY") or []
        if a.get("ActivityName")
    })
    media = [m for m in record.get("MEDIA") or []
             if (m.get("MediaType") or "").upper() == "IMAGE" and m.get("URL")]
    preview = next((m for m in media if m.get("IsPreview")), media[0] if media else None)
    return {
        "activities": activities,
        "photo_url": (preview or {}).get("URL"),
        "photo_credit": (preview or {}).get("Credits") or (preview or {}).get("Title"),
        "description": (record.get("FacilityDescription") or "").strip() or None,
    }


def backfill_facility_details(
    conn: sqlite3.Connection,
    provider: str = "RecreationDotGov",
    states: Optional[list[str]] = None,
    limit: Optional[int] = None,
    fetcher=None,
    now: Optional[datetime] = None,
) -> dict:
    """Activities, photo and description for every federal campground.

    One request each, and it answers three separate questions at once — see
    `fetch_facility_detail`. Like the site inventory this is a maintenance
    job, not part of a scan: a campground's activities change on the order of
    years. Only fills blanks, so a stopped run resumes.
    """
    from . import water

    curated = water.load_curated()
    done = {"visited": 0, "recorded": 0, "photos": 0, "water_yes": 0, "errors": {}}
    campgrounds = [
        cg for cg in store.list_campgrounds(conn, provider=provider, states=states)
        if not getattr(cg, "activities", None)
    ]
    if limit:
        campgrounds = campgrounds[:limit]

    for campground in campgrounds:
        done["visited"] += 1
        try:
            record = fetch_facility_detail(campground.id, fetcher=fetcher)
        except Exception as exc:  # noqa: BLE001 - one bad facility is not fatal
            log.warning("facility detail failed for %s: %s", campground.name, exc)
            done["errors"][campground.name] = str(exc)
            continue
        if not record:
            continue

        detail = parse_facility_detail(record)
        status, evidence = water.derive(
            campground.name, campground.rec_area, detail["activities"],
            curated.get(water.curated_key(campground.provider, campground.id)),
        )
        store.set_facility_detail(
            conn, campground.provider, campground.id,
            water_nearby=status, water_evidence=evidence, now=now, **detail,
        )
        done["recorded"] += 1
        done["photos"] += bool(detail["photo_url"])
        done["water_yes"] += status == water.WATER_YES
    log.info("%s facility details: %s", provider, done)
    return done


#: GoingToCamp park amenities that mean water. Their vocabulary, not ours —
#: read off `/api/attribute/filterable` on 2026-07-31. Note **Boat Launch and
#: Moorage are separate values**, which is the distinction Scott called out as
#: missing from CampSage's Cove Palisades popup.
GTC_WATER_AMENITIES = frozenset({
    "Swimming", "Boat Launch", "Moorage", "Fishing/Shellfishing",
    "Lakes/Rivers/Beach", "Waterfalls",
})
#: Per-site, from the `Adjacent To` attribute.
GTC_WATER_ADJACENCIES = frozenset({"Beach", "Body of Water"})


def backfill_goingtocamp_parks(
    conn: sqlite3.Connection,
    provider: str = "GoingToCamp:WA",
    limit: Optional[int] = None,
    client=None,
    now: Optional[datetime] = None,
) -> dict:
    """Park amenities, photo, description and the water verdict — in 2 requests.

    Not a typo: `/api/resourceLocation` returns **every** location's
    attributes, photos and description in one call, and
    `/api/attribute/filterable` returns the vocabulary to decode them in
    another. 167 Washington parks cost two requests, where the equivalent
    federal pass cost 545.

    This is also the first provider that states the water question outright —
    `Park Amenities` carries Swimming, Boat Launch, Moorage,
    Fishing/Shellfishing, Lakes/Rivers/Beach and Waterfalls — so
    `water_nearby` here rests on the operator's own claim rather than on a
    word in the name.
    """
    from . import water
    from .providers import build_provider

    client = client or build_provider(provider)
    curated = water.load_curated()
    known = {cg.id: cg for cg in store.list_campgrounds(conn, provider=provider)}
    done = {"visited": 0, "recorded": 0, "photos": 0, "water_yes": 0, "errors": {}}

    records = client.location_details()
    if limit:
        records = records[:limit]
    for record in records:
        location_id = record.get("resourceLocationId")
        if location_id is None or str(location_id) not in known:
            continue
        campground = known[str(location_id)]
        done["visited"] += 1
        try:
            decoded = client.decode_attributes(record.get("attributes"))
        except Exception as exc:  # noqa: BLE001 - one bad record is not fatal
            log.warning("attributes failed for %s: %s", campground.name, exc)
            done["errors"][campground.name] = str(exc)
            continue

        amenities = decoded.get("Park Amenities") or []
        photo = _photo_from(record)
        status, evidence = water.derive(
            campground.name, campground.rec_area,
            _water_activities(amenities),
            curated.get(water.curated_key(campground.provider, campground.id)),
        )
        store.set_facility_detail(
            conn, campground.provider, campground.id,
            activities=amenities or None,
            photo_url=photo,
            description=_description_from(record),
            water_nearby=status, water_evidence=evidence, now=now,
        )
        done["recorded"] += 1
        done["photos"] += bool(photo)
        done["water_yes"] += status == water.WATER_YES
    log.info("%s park detail: %s", provider, done)
    return done


def _water_activities(amenities: Iterable[str]) -> list[str]:
    """Amenities that answer the water question, in the platform's own words.

    Passed to `water.derive` as "activities" so the evidence string reads
    "the operator lists Boat Launch, Swimming" — the operator really did.
    """
    return [a for a in amenities or () if a in GTC_WATER_AMENITIES]


def _photo_from(record: dict) -> Optional[str]:
    from .providers.goingtocamp import _photo_of

    return _photo_of(record)


def _description_from(record: dict) -> Optional[str]:
    from .providers.goingtocamp import _description_of

    return _description_of(record)


def backfill_site_inventory(
    conn: sqlite3.Connection,
    provider: str = "RecreationDotGov",
    states: Optional[list[str]] = None,
    limit: Optional[int] = None,
    fetcher=None,
    report: Optional[InventoryReport] = None,
    now: Optional[datetime] = None,
) -> InventoryReport:
    """Record each campground's per-site inventory and its site counts.

    Only fills blanks — a campground already holding site rows is skipped, so
    a long run can be stopped and restarted and picks up where it left off.
    Nothing is written for a campground the source has no site list for;
    unknown stays unknown.
    """
    report = report or InventoryReport()
    source = _source_for(provider, fetcher=fetcher)
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
            records = source.fetch(campground)
        except Exception as exc:  # noqa: BLE001 - one bad park is not fatal
            log.warning("inventory failed for %s: %s", campground.name, exc)
            report.errors[campground.name] = str(exc)
            continue

        if not records:
            # The source has no site list for this campground. That is not
            # "zero sites" — it is "we don't know", and it must not be
            # recorded as an answer. True of every first-come facility.
            report.no_inventory.append(campground.name)
            continue

        counts = source.classify(records)
        if counts.total == 0:
            report.no_inventory.append(campground.name)
            continue

        parsed = [source.parse(r) for r in records if not source.exclude(r)]
        store.upsert_campsites(
            conn, campground.provider, campground.id, parsed,
            source=source.label, now=now,
        )
        store.set_site_inventory(
            conn, campground.provider, campground.id,
            sites_total=counts.total,
            sites_not_bookable=counts.not_bookable,
            site_types=counts.types,
            source=source.label, now=now,
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
