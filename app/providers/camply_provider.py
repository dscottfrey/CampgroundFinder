"""camply adapter (§6) — wraps one camply search class.

VERIFIED against the local clone in `samples/camply-main/` (read-only reference):

  * `CAMPSITE_SEARCH_PROVIDER` is built as
        {provider.provider_class.__name__: provider for provider in __search_providers__}
    (camply/search/__init__.py:57), so its keys are PROVIDER names —
    "RecreationDotGov", "GoingToCamp", "OregonMetro", "Yellowstone", … — and
    NOT search-class names like "SearchRecreationDotGov". The build plan's §6
    and §12 both say search-class name; that is incorrect against this version.
    We accept either spelling and normalize, so config written to the plan's
    wording still works.
  * `SearchRecreationDotGov.__init__` takes `recreation_area`, `campgrounds`,
    `campsites` as real (non-kwargs) params — confirmed at
    camply/search/search_recreationdotgov.py:41.
  * `get_matching_campsites(log, verbose, continuous, polling_interval,
    notification_provider, notify_first_try, search_forever, search_once)`
    returns `List[AvailableCampsite]` — confirmed at base_search.py:531.

BUILD NOTE (§6): the constructor kwargs above are verified for RecreationDotGov.
Some UseDirect / GoingToCamp subclasses differ — confirm against the clone
before enabling a non-federal source.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .base import Campground, Campsite, Provider, SearchRequest

log = logging.getLogger(__name__)

#: Seconds between paged directory requests. Deliberately unhurried — the
#: catalog scrape is the heavy one, it runs at most a few times a year, and it
#: must never cost us the home IP (§13).
DIRECTORY_PAGE_DELAY = 2.0
DIRECTORY_PAGE_SIZE = 50


class CamplyNotInstalled(RuntimeError):
    pass


def _load_camply():
    """Lazy import so the app boots (and tests run) without camply installed."""
    try:
        from camply.containers import SearchWindow
        from camply.search import CAMPSITE_SEARCH_PROVIDER
    except ImportError as exc:  # pragma: no cover - exercised via fake module
        raise CamplyNotInstalled(
            "camply is not installed — `pip install -r requirements.txt`"
        ) from exc
    return CAMPSITE_SEARCH_PROVIDER, SearchWindow


class CamplyProvider(Provider):
    """Wraps one camply search class (e.g. provider name "RecreationDotGov")."""

    def __init__(self, provider_name: str, state: Optional[str] = None):
        # Accept both the verified key ("RecreationDotGov") and the plan's
        # search-class spelling ("SearchRecreationDotGov").
        self.provider_name = self._normalize(provider_name)
        self.name = self.provider_name
        # camply's payload doesn't reliably carry a state, so the source config
        # is the source of truth for the region selector (§6 build note).
        self.state = state

    @staticmethod
    def _normalize(provider_name: str) -> str:
        if provider_name.startswith("Search"):
            return provider_name[len("Search"):]
        return provider_name

    def _search_class(self):
        registry, _ = _load_camply()
        try:
            return registry[self.provider_name]
        except KeyError as exc:
            raise KeyError(
                f"unknown camply provider {self.provider_name!r}; "
                f"available: {sorted(registry)}"
            ) from exc

    def search(self, req: SearchRequest) -> list[Campsite]:
        _, SearchWindow = _load_camply()
        search_cls = self._search_class()
        window = SearchWindow(start_date=req.start_date, end_date=req.end_date)

        finder = search_cls(
            search_window=window,
            recreation_area=req.rec_area_ids or None,
            campgrounds=req.campground_ids or None,
            campsites=req.campsite_ids or None,
            weekends_only=req.weekends_only,
            nights=req.nights,
            offline_search=False,
        )
        # One-shot search. continuous=False returns a plain list.
        found = finder.get_matching_campsites(
            log=False, verbose=False, continuous=False, notification_provider="silent",
        )
        return [self._normalize_site(c) for c in found]

    def _normalize_site(self, c) -> Campsite:
        loc = getattr(c, "location", None)
        booking_date = c.booking_date
        available_date = (
            booking_date.date() if hasattr(booking_date, "date") else booking_date
        )
        equipment = getattr(c, "permitted_equipment", None) or []
        return Campsite(
            provider=self.name,
            campsite_id=str(c.campsite_id),
            available_date=available_date,
            nights=c.booking_nights,
            site_name=c.campsite_site_name,
            loop=c.campsite_loop_name,
            campsite_type=c.campsite_type,
            status=c.availability_status,
            rec_area=c.recreation_area,
            rec_area_id=str(c.recreation_area_id),
            facility_name=c.facility_name,
            facility_id=str(c.facility_id),
            booking_url=c.booking_url,
            state=self.state,
            latitude=getattr(loc, "latitude", None) if loc else None,
            longitude=getattr(loc, "longitude", None) if loc else None,
            extra={
                "permitted_equipment": [
                    getattr(e, "equipment_name", str(e)) for e in equipment
                ]
            },
        )

    # -- catalog enumeration (§8k) -----------------------------------------

    def list_campgrounds(
        self,
        state: Optional[str] = None,
        rec_area_ids: Optional[list[str]] = None,
    ) -> list[Campground]:
        """Enumerate the provider's full campground directory via camply (§8k).

        Delegates to the provider client's `find_campgrounds`, never a
        hand-picked list — a shortlist here is exactly the Reehers failure.

        Signatures differ per provider (verified in the clone): RecreationDotGov
        takes `rec_area_id` and ignores `state`; UseDirect providers take both.
        We pass whichever we have and let each absorb the rest via **kwargs.
        """
        search_cls = self._search_class()
        provider_client = getattr(search_cls, "provider_class", None)
        if provider_client is None:
            return []
        client = provider_client()
        resolved_state = state or self.state

        # RecreationDotGov: go straight to the RIDB facilities directory, which
        # is the only call that filters by state AND returns coordinates.
        # camply's own find_campgrounds() ignores `state` and yields no lat/lon.
        if self.provider_name == "RecreationDotGov" and resolved_state:
            return self._list_recdotgov_by_state(client, resolved_state)

        finder = getattr(client, "find_campgrounds", None)
        if finder is None:
            return []
        kwargs: dict = {"search_string": None}
        if resolved_state:
            kwargs["state"] = resolved_state
        if rec_area_ids:
            kwargs["rec_area_id"] = [int(r) for r in rec_area_ids]
        facilities = finder(**kwargs) or []
        return [self._normalize_campground(f, resolved_state) for f in facilities]

    def _list_recdotgov_by_state(self, client, state: str) -> list[Campground]:
        """Page the RIDB `facilities` directory for one state, gently.

        Enumerates the WHOLE directory for the state — never a shortlist (§8k).
        Sleeps between pages; this runs a few times a year, not continuously.
        """
        out: list[Campground] = []
        offset = 0
        total = None
        while True:
            payload = client.get_ridb_data(
                "facilities",
                {
                    "state": state,
                    "activity": "CAMPING",
                    "limit": DIRECTORY_PAGE_SIZE,
                    "offset": offset,
                },
            )
            if not isinstance(payload, dict):
                break
            records = payload.get("RECDATA") or []
            if total is None:
                total = (
                    payload.get("METADATA", {})
                    .get("RESULTS", {})
                    .get("TOTAL_COUNT")
                )
                log.info("RIDB %s: %s camping facilities", state, total)
            for rec in records:
                cg = self._campground_from_ridb(rec, state)
                if cg:
                    out.append(cg)
            offset += DIRECTORY_PAGE_SIZE
            if not records or (total is not None and offset >= total):
                break
            time.sleep(DIRECTORY_PAGE_DELAY)
        return out

    @staticmethod
    def _campground_from_ridb(rec: dict, state: str) -> Optional[Campground]:
        facility_id = rec.get("FacilityID")
        name = (rec.get("FacilityName") or "").strip()
        if not facility_id or not name:
            return None

        def _coord(value):
            try:
                num = float(value)
            except (TypeError, ValueError):
                return None
            # RIDB uses 0.0 as a null sentinel for missing coordinates.
            return None if num == 0 else num

        reservable = bool(rec.get("Reservable"))
        return Campground(
            provider="RecreationDotGov",
            id=str(facility_id),
            name=name,
            rec_area=rec.get("FacilityTypeDescription") or None,
            state=state,
            latitude=_coord(rec.get("FacilityLatitude")),
            longitude=_coord(rec.get("FacilityLongitude")),
            # Not reservable through recreation.gov = first-come. Still
            # catalogued and shown, just with no booking link (§4).
            reservation_type="reservable" if reservable else "first_come",
        )

    def _normalize_campground(self, facility, state: Optional[str]) -> Campground:
        # NOTE: `CampgroundFacility.coordinates` is declared in camply's
        # container but never populated by any provider in this version — so
        # camply enumeration yields no lat/lon. Those come from the seed or a
        # RIDB lookup; until then the pin is "location unknown" (§13), which
        # is a legitimate state, not a reason to drop it.
        coords = getattr(facility, "coordinates", None) or (None, None)
        return Campground(
            provider=self.name,
            id=str(facility.facility_id),
            name=facility.facility_name,
            rec_area=getattr(facility, "recreation_area", None),
            state=state,
            latitude=coords[0],
            longitude=coords[1],
        )
