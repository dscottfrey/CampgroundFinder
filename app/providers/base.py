"""Normalized provider interface — the core extensibility point (§5).

Everything a source returns is normalized into `Campsite` (availability) and
`Campground` (catalog), so the map, filters, and alerts never care where a
record came from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class SearchRequest:
    provider: str                     # e.g. "RecreationDotGov" or "PerfectMind:SanJuanCoWA"
    start_date: date
    end_date: date
    nights: int = 1
    weekends_only: bool = False
    rec_area_ids: list[str] = field(default_factory=list)
    campground_ids: list[str] = field(default_factory=list)
    campsite_ids: list[str] = field(default_factory=list)


@dataclass
class Campsite:                        # normalized availability record
    provider: str
    campsite_id: str
    available_date: date               # first night of the run
    nights: int
    site_name: str
    loop: Optional[str] = None
    campsite_type: Optional[str] = None
    status: str = "available"
    reservation_type: str = "reservable"  # 'reservable' | 'first_come' (FCFS: status, no booking link — §4)
    rec_area: Optional[str] = None
    rec_area_id: Optional[str] = None
    facility_name: Optional[str] = None
    facility_id: Optional[str] = None
    booking_url: Optional[str] = None
    state: Optional[str] = None        # region code: "OR", "WA", "BC", "CA-NAT" — drives the region selector
    aqi_status: Optional[str] = None   # 'green' | 'not_green' | 'tbd' | 'unknown' (§8d enricher, step 4)
    fire_status: Optional[str] = None  # 'clear' | 'near' | 'unknown' (§8e enricher, step 4)
    attributes: dict = field(default_factory=dict)  # normalized; null value = unknown (§8f/§8g)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    extra: dict = field(default_factory=dict)   # provider-specific: rating, equipment, price…

    @property
    def key(self) -> str:              # stable identity for dedupe/notify
        return f"{self.provider}|{self.campsite_id}|{self.available_date}|{self.nights}"


# Catalog statuses (§8k). `unknown`/`stale` mean "we couldn't confirm", never
# "it doesn't exist" — a catalogued campground is never dropped from the map.
STATUS_AVAILABLE = "available"
STATUS_FULL = "full"
STATUS_CLOSED = "closed"
STATUS_UNKNOWN = "unknown"
STATUS_STALE = "stale"


@dataclass
class Campground:
    """One row of the known universe (§8k) — the map is drawn from these."""

    provider: str
    id: str                            # provider-native id (e.g. ReserveAmerica parkId 412704)
    name: str
    rec_area: Optional[str] = None
    state: Optional[str] = None
    latitude: Optional[float] = None   # None is legitimate — show "location unknown", don't drop (§13)
    longitude: Optional[float] = None
    #: What the campground AS A WHOLE is: 'reservable' (it takes bookings) or
    #: 'first_come' (it takes none at all). This is a different claim from the
    #: one below, and conflating them is how you tell someone a bookable
    #: campground has no walk-up sites when you never checked.
    reservation_type: str = "reservable"
    #: Whether a *reservable* campground ALSO holds first-come sites.
    #: Three-state (§8g): True = known to, False = known not to,
    #: **None = we don't know**, which is the honest default almost everywhere.
    #: Never inferred from `reservation_type` — the two are independent.
    first_come_sites: Optional[bool] = None
    status: str = STATUS_UNKNOWN
    status_reason: Optional[str] = None
    closed_until: Optional[str] = None
    #: Where the coordinate came from, when it did not come from this
    #: provider's own enumeration (see app/coordinates.py). None means the
    #: provider supplied it, or there is no coordinate at all.
    coord_source: Optional[str] = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider, self.id)

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def booking_label(self) -> str:
        """One honest sentence about how you get a site here.

        Says nothing about walk-up sites when `first_come_sites` is None —
        silence, not a cheerful "all sites reservable" we cannot support.
        """
        if self.reservation_type == "first_come":
            return "First-come, first-served — no reservations"
        if self.first_come_sites is True:
            return "Reservable, and some sites are first-come"
        if self.first_come_sites is False:
            return "Reservable — every site is bookable"
        return "Reservable"


class Provider(ABC):
    name: str
    #: The host this provider talks to, if it is known — the key the shared
    #: rate limiter spaces requests by (docs/scanning-design.md). `None` is
    #: honest for providers that reach several hosts or none at all; the
    #: limiter then paces them by provider name at the conservative default.
    host: Optional[str] = None
    #: True when `search()` refuses to run without `campground_ids`. Such a
    #: provider gets its scope filled in from the catalog — the known universe
    #: (§8k) — rather than from a hand-written list in config, which is how a
    #: shortlist creeps back in.
    requires_scope: bool = False

    @abstractmethod
    def search(self, req: SearchRequest) -> list[Campsite]:
        ...

    def list_campgrounds(
        self,
        state: Optional[str] = None,
        rec_area_ids: Optional[list[str]] = None,
    ) -> list[Campground]:
        """Enumerate this provider's full campground directory (§8k).

        Optional: providers that can't enumerate return [] and contribute
        nothing to the catalog beyond what the seed already holds. Never
        return a hand-picked shortlist here — a shortlist is exactly how
        Reehers vanished from CampSage's map.
        """
        return []
