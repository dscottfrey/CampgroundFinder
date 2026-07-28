"""Coordinate backfill — locating campgrounds a provider can't locate itself.

Deliberately **not** part of the scan cycle. Coordinates change approximately
never, so this is a maintenance job you run by hand or on a slow schedule:

    python3 scripts/manage.py backfill-coordinates --provider GoingToCamp:BC

## Why BC needs it

The GoingToCamp platform publishes `gpsCoordinates` for 78 of Washington's 79
parks and for **1 of British Columbia's 114**. Those parks were catalogued and
honestly flagged "location unknown" (§8k/§13) — present and searchable, but off
the map, which is most of what the app is for.

## Where the coordinates come from

The **BC Parks API** (`bcparks.api.gov.bc.ca`), the province's own open REST
service, licensed under the Open Government Licence — British Columbia. It is
the operator's own data rather than a scrape of anyone's page, which is why it
is the first choice: authoritative, licensed for this use, and re-fetchable.

The join key is exact, not fuzzy: every GoingToCamp record carries a canonical
`bcparks.ca/<slug>/` website, and every API record carries the same URL. We
match on the slug and nothing else. **Name matching is not attempted** — the
two systems disagree ("Birkenhead Lake Provincial Park" vs "Birkenhead Lake
Park"), and a fuzzy match that lands on the wrong park is worse than no
coordinate at all.

Measured 2026-07-28: 110 of 114 located. The four that don't match stay
unlocated on purpose — a renamed park, one unlisted, and two Wells Gray
sub-areas whose parent park is enormous, so the parent's centroid would put a
camper tens of kilometres from the campground.

## Pagination, and a trap worth remembering

The API is Strapi. It paginates on `pagination[page]` / `pagination[pageSize]`,
**silently ignores** `offset` and `_start` (they always return page 1), and its
default ordering is not stable across pages — paging without an explicit sort
returned 1052 rows containing only 736 distinct parks, so a third of the
province went missing while duplicates filled the gap. Always sort. The count
is checked against the API's own `meta.pagination.total` below, because a
silently short answer is the failure mode this project keeps meeting.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import store
from .pacing import shared_limiter

log = logging.getLogger(__name__)

BC_PARKS_API_HOST = "bcparks.api.gov.bc.ca"
BC_PARKS_SOURCE = "BC Parks API (Open Government Licence — BC)"
PAGE_SIZE = 100


class IncompleteSource(RuntimeError):
    """The upstream list came back short, so we don't trust any of it."""


@dataclass
class BackfillReport:
    considered: int = 0
    located: int = 0
    unmatched: list[str] = field(default_factory=list)
    source_records: int = 0

    def summary(self) -> str:
        return (
            f"considered={self.considered} located={self.located} "
            f"unmatched={len(self.unmatched)} source={self.source_records}"
        )


def park_slug(url: Optional[str]) -> Optional[str]:
    """`https://bcparks.ca/ruckle-park/` -> `ruckle-park`."""
    if not url:
        return None
    slug = str(url).strip().rstrip("/").rsplit("/", 1)[-1].lower()
    return slug or None


def fetch_bc_protected_areas(fetcher=None) -> list[dict]:
    """Every BC protected area, sorted so paging is deterministic.

    Raises `IncompleteSource` if fewer distinct records arrive than the API
    says exist — a partial reference list would silently mislocate nothing but
    would silently *fail to* locate a third of the province.
    """
    fetch = fetcher or _http_get_json
    areas: dict[int, dict] = {}
    page, pages, total = 1, None, None
    while pages is None or page <= pages:
        payload = fetch(
            "/api/protected-areas",
            {
                "pagination[page]": page,
                "pagination[pageSize]": PAGE_SIZE,
                # Without this the ordering wanders between pages and the walk
                # returns duplicates instead of the tail. See module docstring.
                "sort[0]": "id:asc",
            },
        )
        for record in payload.get("data") or []:
            areas[record.get("id")] = record
        pagination = (payload.get("meta") or {}).get("pagination") or {}
        pages = pagination.get("pageCount", page)
        total = pagination.get("total", total)
        page += 1

    if total is not None and len(areas) < total:
        raise IncompleteSource(
            f"BC Parks API reported {total} protected areas but only "
            f"{len(areas)} distinct ones arrived — refusing to backfill from a "
            f"partial list."
        )
    log.info("BC Parks API: %d protected areas", len(areas))
    return list(areas.values())


def _http_get_json(path: str, params: dict) -> dict:
    import requests

    limiter = shared_limiter()
    with limiter.slot(BC_PARKS_API_HOST, label="BC Parks API"):
        response = requests.get(
            f"https://{BC_PARKS_API_HOST}{path}",
            params=params,
            headers={
                "User-Agent": (
                    "CampgroundFinder/0.1 (personal campsite availability "
                    "tracker; low volume)"
                ),
                "Accept": "application/json",
            },
            timeout=40,
        )
    response.raise_for_status()
    return response.json()


def backfill_bc(
    conn: sqlite3.Connection,
    provider: str = "GoingToCamp:BC",
    websites: Optional[dict[str, str]] = None,
    fetcher=None,
    now: Optional[datetime] = None,
) -> BackfillReport:
    """Locate BC campgrounds from the province's own API.

    `websites` maps campground id -> its `bcparks.ca` URL. It comes from the
    GoingToCamp directory; pass it in so this function needs no second provider
    and stays testable offline.
    """
    report = BackfillReport()
    areas = fetch_bc_protected_areas(fetcher=fetcher)
    report.source_records = len(areas)
    by_slug = {
        park_slug(a.get("url")): a
        for a in areas
        if a.get("url") and a.get("latitude") is not None
    }

    websites = websites or {}
    for campground in store.list_campgrounds(conn, provider=provider):
        if campground.has_location:
            continue                      # never overwrite a coordinate we have
        report.considered += 1
        area = by_slug.get(park_slug(websites.get(campground.id)))
        if not area:
            # No exact match. We do NOT fall back to name matching: the two
            # systems name parks differently, and a near-miss would place a
            # pin on the wrong park.
            report.unmatched.append(campground.name)
            continue
        if store.set_campground_coordinates(
            conn, campground.provider, campground.id,
            area.get("latitude"), area.get("longitude"),
            source=BC_PARKS_SOURCE, now=now,
        ):
            report.located += 1
    log.info("%s backfill: %s", provider, report.summary())
    return report
