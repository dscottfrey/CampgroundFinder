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
import re
from typing import Optional

from ..pacing import RateLimiter, shared_limiter
from .base import Campground, Campsite, Provider, SearchRequest

log = logging.getLogger(__name__)

#: Hosts this adapter talks to, for the shared limiter. camply owns the socket,
#: so we can only pace it from the outside — by holding the process's request
#: slot around each call it makes.
RIDB_HOST = "ridb.recreation.gov"

#: Only filled in where the host is actually known. An unlisted provider is
#: paced by its own name at the limiter's conservative default rather than
#: being guessed into somebody else's budget.
PROVIDER_HOSTS = {
    "RecreationDotGov": RIDB_HOST,
    "RecreationDotGovDailyTicket": RIDB_HOST,
    "RecreationDotGovDailyTimedEntry": RIDB_HOST,
    "RecreationDotGovTicket": RIDB_HOST,
    "RecreationDotGovTimedEntry": RIDB_HOST,
}

#: Seconds between paged directory requests, documented here but enforced by
#: `pacing.HOST_DELAYS`. Deliberately unhurried — the catalog scrape is the
#: heavy one, it runs at most a few times a year, and it must never cost us the
#: home IP (§13).
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


#: Facility types RIDB uses for places you can sleep. `Campground` is the
#: obvious one; `Facility` is the catch-all that Heart O' the Hills and most
#: of Olympic's campgrounds sit in, so it cannot simply be excluded.
_CAMPGROUND_TYPES = frozenset({"campground", "campsite"})

#: Names that mean "somewhere to camp" when the type is the vague `Facility`.
#: Deliberately narrow: this decides what enters the catalog, and a loose
#: match would fill the map with trailheads and boat ramps.
_CAMP_NAME = re.compile(
    r"\b(campground|camp ground|campsites?|horse camp|group camp|"
    r"tent camp|rv park|campomat)\b", re.I)

#: Facility types that are never a place to sleep, whatever they are called.
_NEVER_CAMPING = frozenset({"permit", "ticket facility", "tour"})


def looks_like_a_campground(rec: dict) -> bool:
    """Is this RIDB facility somewhere you can spend the night?

    Replaces the `activity=CAMPING` query filter, which silently lost four
    national parks because RIDB's activity tagging is optional and agencies
    frequently skip it (see `_list_recdotgov_by_state`).

    The test is deliberately evidence-based and ordered from strongest to
    weakest: the facility's own type, then its stated activities, then its
    name. A campground that fails all three is genuinely indistinguishable
    from a boat ramp in this data.
    """
    kind = (rec.get("FacilityTypeDescription") or "").strip().lower()
    if kind in _NEVER_CAMPING:
        return False
    if kind in _CAMPGROUND_TYPES:
        return True
    activities = {
        (a.get("ActivityName") or "").strip().upper()
        for a in rec.get("ACTIVITY") or []
    }
    if "CAMPING" in activities:
        return True
    return bool(_CAMP_NAME.search(rec.get("FacilityName") or ""))


class CamplyProvider(Provider):
    """Wraps one camply search class (e.g. provider name "RecreationDotGov")."""

    def __init__(
        self,
        provider_name: str,
        state: Optional[str] = None,
        limiter: Optional[RateLimiter] = None,
    ):
        # Accept both the verified key ("RecreationDotGov") and the plan's
        # search-class spelling ("SearchRecreationDotGov").
        self.provider_name = self._normalize(provider_name)
        self.name = self.provider_name
        # camply's payload doesn't reliably carry a state, so the source config
        # is the source of truth for the region selector (§6 build note).
        self.state = state
        self.limiter = limiter or shared_limiter()
        self.host = PROVIDER_HOSTS.get(self.provider_name)
        #: What the limiter spaces by. Falls back to the provider name so an
        #: unmapped provider still gets its own bucket — at the default 6s,
        #: not at RIDB's 2s, because we don't know whose door we're knocking on.
        self._pacing_key = self.host or self.provider_name

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
        #
        # camply owns the HTTP here and fires several requests inside this one
        # call, so we cannot space them individually. Holding the process's
        # request slot for the whole call is what we *can* do: it guarantees no
        # other provider adds traffic while camply is talking, and it books the
        # per-host gap afterwards. camply's own internal pacing is unverified.
        with self.limiter.slot(self._pacing_key, label=self.name):
            found = finder.get_matching_campsites(
                log=False, verbose=False, continuous=False,
                notification_provider="silent",
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

        # RecreationDotGov: the state directory PLUS any configured rec areas,
        # unioned.
        #
        # The state walk alone is not enough, and this cost four national
        # parks. Measured 2026-07-31: `facilities?state=WA` returns 485
        # facilities and **Heart O' the Hills is not among them**, even though
        # its own address reads WA — RIDB's state filter evidently indexes
        # something other than the address. Asking its rec area directly
        # (`recareas/2881/facilities`) returns it immediately.
        #
        # So neither call is complete on its own, and the union is the honest
        # answer: the state sweep for breadth, the rec areas for the places we
        # know we care about. This is why `rec_area_ids` exists in the config;
        # the previous version accepted them and then ignored them for this
        # provider, which is worse than not offering the setting at all.
        if self.provider_name == "RecreationDotGov":
            found: dict[str, Campground] = {}
            if resolved_state:
                for cg in self._list_recdotgov_by_state(client, resolved_state):
                    found[cg.id] = cg
            for rec_area_id in rec_area_ids or []:
                for cg in self._list_recdotgov_by_rec_area(
                        client, str(rec_area_id), resolved_state):
                    found.setdefault(cg.id, cg)
            if found:
                return list(found.values())

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
        Paced by the shared limiter; this runs a few times a year, not
        continuously.

        ## Why this does NOT filter on `activity=CAMPING`

        It used to, and it cost us four national parks. Measured 2026-07-31,
        after Scott noticed Heart O' the Hills was missing:

            facilities?state=WA&activity=CAMPING  ->  191
            facilities?state=WA                   ->  485

        191 is exactly what we held. **Heart O' the Hills has an empty
        ACTIVITY array** — the agency never tagged it — and so did every
        campground in Olympic (12), North Cascades (9) and Crater Lake (2).
        Mount Rainier kept 4 of its 5 by luck.

        The lesson is the one this project keeps relearning: **never filter on
        optional metadata the source does not reliably populate.** An untagged
        campground is not a non-campground. So the walk now takes the whole
        state directory and decides what a campground is from evidence it can
        actually see, counting what it rejects so the gap stays visible rather
        than silent (§8k).
        """
        out: list[Campground] = []
        offset = 0
        total = None
        skipped = 0
        while True:
            with self.limiter.slot(self._pacing_key, label=f"{self.name} directory {state}"):
                payload = client.get_ridb_data(
                    "facilities",
                    {
                        "state": state,
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
                if not looks_like_a_campground(rec):
                    skipped += 1
                    continue
                cg = self._campground_from_ridb(rec, state)
                if cg:
                    out.append(cg)
            offset += DIRECTORY_PAGE_SIZE
            if not records or (total is not None and offset >= total):
                break
            # No sleep here — the limiter above already spaced this loop, and
            # sleeping twice would silently double the documented interval.
        log.info("RIDB %s: kept %d campgrounds, skipped %d other facilities "
                 "(boat launches, trailheads, offices)", state, len(out), skipped)
        return out

    def _list_recdotgov_by_rec_area(
        self, client, rec_area_id: str, state: Optional[str]
    ) -> list[Campground]:
        """Every campground in one recreation area, paged and count-checked.

        The reliable half of the union — see `list_campgrounds`. A rec area
        knows its own facilities even where the state index does not.
        """
        out: list[Campground] = []
        offset, total = 0, None
        while True:
            with self.limiter.slot(self._pacing_key,
                                   label=f"{self.name} rec area {rec_area_id}"):
                payload = client.get_ridb_data(
                    f"recareas/{rec_area_id}/facilities",
                    {"limit": DIRECTORY_PAGE_SIZE, "offset": offset},
                )
            if not isinstance(payload, dict):
                break
            records = payload.get("RECDATA") or []
            if total is None:
                total = (payload.get("METADATA", {})
                         .get("RESULTS", {}).get("TOTAL_COUNT"))
            for rec in records:
                if not looks_like_a_campground(rec):
                    continue
                cg = self._campground_from_ridb(rec, state or self.state)
                if cg:
                    out.append(cg)
            offset += DIRECTORY_PAGE_SIZE
            if not records or (total is not None and offset >= total):
                break
        log.info("RIDB rec area %s: %d campgrounds of %s facilities",
                 rec_area_id, len(out), total)
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
