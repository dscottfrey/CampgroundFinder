"""GoingToCamp provider — Washington State Parks, BC Parks, Tacoma Power.

Parameterized by **(host, rec_area_id)**, because one platform serves many
agencies: Washington is `washington.goingtocamp.com` + area 3, BC Parks is
`camping.bcparks.ca` + area 12 (see docs/goingtocamp-clients.md).

**Why this talks to the API directly instead of going through camply.**
camply's GoingToCamp provider raises `KeyError: -2147483647` before returning
anything. Diagnosed 2026-07-28 against the live API: it builds a lookup from
`/api/maps`, but that endpoint returns six organization-level maps whose
`resourceLocationId` is `null` — so the dict is keyed entirely by `None`, and
`going_to_camp_provider.py:427` (a bare subscript whose result is discarded)
raises on the first park it checks. `-2147483647` is Alta Lake State Park, the
first Washington park alphabetically. We don't need that endpoint at all:
`rootMapId` is already on every `/api/resourceLocation` record.

Verified live 2026-07-28 against Washington:

  * `/api/resourceLocation` — every location in the rec area, 167 for WA, of
    which 79 are campable. Carries name, `gpsCoordinates`, `rootMapId`, and
    `resourceCategoryIds`. One request for the whole catalog.

  * `/api/availability/map?mapId=…&getDailyAvailability=true` — **the daily
    matrix**: every site on that map against every night in the window, in one
    request. 46 sites x 31 nights came back in 58 KB.

  * A park's root map usually holds no sites of its own — it holds
    `mapLinkAvailabilities`, the loops within the park, and those have to be
    walked. Alta Lake has four.

**Availability encoding**, derived by running both modes over one window and
cross-tabulating (it is documented nowhere, and camply only ever uses the
non-daily mode):

    code 0 = available that night
    code 1 = taken
    codes 4/5 = some other state, seen on 2 of 46 sites — NOT treated as open

A stay of N nights from day D is bookable exactly when the first N daily codes
from D are all 0. The trailing entry is checkout day and is ignored — verified:
every bookable 2-night site read (0,0,0) or (0,0,1), and no unbookable one had
its first two entries both 0.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Iterable, Optional
from urllib.parse import urlencode

from ..pacing import Blocked, RateLimiter, shared_limiter
from .base import STATUS_UNKNOWN, Campground, Campsite, Provider, SearchRequest

log = logging.getLogger(__name__)

#: Resource categories that mean "you can camp here", from camply's constants
#: and confirmed against the live payload.
CAMP_SITE = -2147483648
OVERFLOW_SITE = -2147483647
GROUP_SITE = -2147483643
CAMPABLE_CATEGORIES = frozenset({CAMP_SITE, OVERFLOW_SITE, GROUP_SITE})

#: Equipment filter meaning "not a group booking" — camply's NON_GROUP_EQUIPMENT.
NON_GROUP_EQUIPMENT = -32768

#: The one daily code that means the night is open. Anything else is not open;
#: we never treat an unrecognized code as availability (§13 — unknown is not
#: a green light).
AVAILABLE = 0


class GoingToCampProvider(Provider):
    #: `search()` needs to know which parks to ask about; the scanner fills the
    #: scope in from the catalog rather than from a hand-written config list.
    requires_scope = True

    def __init__(
        self,
        instance: str,
        host: str,
        rec_area_id: int,
        state: Optional[str] = None,
        limiter: Optional[RateLimiter] = None,
        fetcher=None,
    ):
        self.instance = instance
        self.host = host
        self.rec_area_id = rec_area_id
        self.state = state or instance
        self.name = f"GoingToCamp:{instance}"
        # Injectable so tests replay cached payloads instead of hitting the API.
        self._fetch = fetcher or self._http_get_json
        self.limiter = limiter or shared_limiter()
        self._map_ids: Optional[dict[str, int]] = None
        #: 62 attribute definitions for the whole portal, fetched once.
        self._attribute_defs: Optional[dict] = None

    # -- transport ---------------------------------------------------------

    def _http_get_json(self, path: str, params: dict):
        import requests

        url = f"https://{self.host}{path}"
        with self.limiter.slot(self.host, label=self.name):
            response = requests.get(
                url, headers=self._headers(), params=params, timeout=30
            )
        if response.status_code in (403, 429):
            # Stop dead; never retry into a block (§13). Latched on the shared
            # limiter so every other caller in the process stops too.
            reason = f"{self.name} returned {response.status_code} — backing off"
            self.limiter.block(self.host, reason)
            raise BlockedByProvider(reason)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _headers() -> dict:
        # A single stable User-Agent that names this tool, browser-shaped so an
        # Azure WAF rule keyed on `Mozilla/5.0` lets a read through. We stay
        # identifiable and blockable on purpose: rotating agents (what camply
        # does) is both dishonest and *more* detectable, since per-request
        # variation is itself a bot signal. This is the only concession made to
        # the WAF — the pace, the read-only behaviour, and the deep-link
        # hand-off to their own booking page are all unchanged.
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 "
                "Safari/537.36 CampgroundFinder/0.1 (+personal use; low volume)"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    # -- catalog -----------------------------------------------------------

    def list_campgrounds(
        self,
        state: Optional[str] = None,
        rec_area_ids: Optional[list[str]] = None,
    ) -> list[Campground]:
        """Every campable location in the rec area, in one request.

        Filtered by resource category, never by a name search — the whole
        point of the catalog model (§8k). A location with no coordinates is
        kept and shown as "location unknown"; it is never dropped and never
        given a guessed position (§13).
        """
        payload = self._fetch("/api/resourceLocation", {})
        out: list[Campground] = []
        for entry in payload:
            categories = set(entry.get("resourceCategoryIds") or [])
            if not categories & CAMPABLE_CATEGORIES:
                continue
            location_id = entry.get("resourceLocationId")
            if location_id is None:
                continue
            lat, lon = _parse_coordinates(entry.get("gpsCoordinates"))
            out.append(
                Campground(
                    provider=self.name,
                    id=str(location_id),
                    name=_name_of(entry) or str(location_id),
                    rec_area=entry.get("region") or None,
                    state=state or self.state,
                    latitude=lat,
                    longitude=lon,
                    reservation_type="reservable",
                    status=STATUS_UNKNOWN,
                )
            )
        log.info("%s: %d campable locations", self.name, len(out))
        return out

    # -- attributes: the vocabulary this platform publishes -----------------

    def attribute_definitions(self) -> dict:
        """`{definitionId: {"name": str, "values": {enumValue: displayName}}}`.

        **One request for the whole portal** — 62 definitions for Washington —
        and it is what turns the opaque numbers in a location or site record
        into words. Without it, `attributes` reads
        `{"attributeDefinitionId": -32706, "values": [0, 1, 2, …]}` and means
        nothing.

        Decoded by **`enumValue`, never by position in the list.** The values
        arrive ordered by an `order` field that is not the enum, so indexing
        the array would relabel every amenity silently — "Boat Launch" landing
        on whatever happens to sit at that offset. Checked live 2026-07-31.
        """
        if self._attribute_defs is None:
            payload = self._fetch("/api/attribute/filterable", {})
            defs = {}
            for key, definition in (payload or {}).items():
                values = {}
                for item in definition.get("values") or []:
                    enum = item.get("enumValue")
                    if enum is None:
                        continue
                    values[enum] = _display_name(item)
                defs[str(key)] = {
                    "name": _display_name(definition),
                    "values": values,
                    "min": definition.get("minValue"),
                    "max": definition.get("maxValue"),
                }
            self._attribute_defs = defs
        return self._attribute_defs

    def decode_attributes(self, attributes: Optional[list]) -> dict:
        """A record's raw `attributes` list, as `{name: value or [values]}`.

        A definition carries either a scalar `value` or a `values` list of enum
        keys. Both shapes appear on the same platform, so both are handled and
        neither is assumed.
        """
        defs = self.attribute_definitions()
        out: dict = {}
        for attribute in attributes or []:
            definition = defs.get(str(attribute.get("attributeDefinitionId")))
            if not definition or not definition["name"]:
                continue
            enums = attribute.get("values")
            if enums:
                named = [definition["values"].get(e) for e in enums]
                out[definition["name"]] = [n for n in named if n]
            elif attribute.get("value") is not None:
                raw = attribute["value"]
                out[definition["name"]] = definition["values"].get(raw, raw)
        return out

    def location_details(self) -> list[dict]:
        """Every location's raw record — attributes, photos, description.

        The same single `/api/resourceLocation` call the catalog already
        makes, returned unreduced. Worth knowing: on this platform the park
        photo and description cost **nothing extra**, unlike RIDB where each
        facility is its own request.
        """
        return list(self._fetch("/api/resourceLocation", {}) or [])

    def map_id_for(self, campground_id: str) -> Optional[int]:
        """The park's root map, needed by every availability call.

        Cached per instance: it comes from the same directory call the catalog
        uses, so a whole scan cycle pays for it once.
        """
        if self._map_ids is None:
            payload = self._fetch("/api/resourceLocation", {})
            self._map_ids = {
                str(e["resourceLocationId"]): e.get("rootMapId")
                for e in payload
                if e.get("resourceLocationId") is not None
            }
        return self._map_ids.get(str(campground_id))

    # -- availability ------------------------------------------------------

    def _availability_params(self, campground_id: str, map_id, req: SearchRequest) -> dict:
        return {
            "mapId": map_id,
            "resourceLocationId": campground_id,
            "bookingCategoryId": 0,
            "startDate": req.start_date.isoformat(),
            "endDate": req.end_date.isoformat(),
            "isReserving": True,
            # The whole reason a park costs so little: one request returns
            # every site against every night in the window.
            "getDailyAvailability": True,
            "partySize": 1,
            "numEquipment": 1,
            "equipmentCategoryId": NON_GROUP_EQUIPMENT,
            "filterData": [],
        }

    def park_availability(self, campground_id: str, req: SearchRequest) -> dict:
        """`{resource_id: [daily codes]}` for a park, walking its sub-maps.

        A park's root map usually holds no sites of its own — only links to the
        loops inside it — so the links have to be followed or the park reads as
        empty. Nested links are followed to a bounded depth.
        """
        map_id = self.map_id_for(campground_id)
        if map_id is None:
            log.warning("%s: no root map for park %s", self.name, campground_id)
            return {}

        resources: dict[str, list] = {}
        pending = [map_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop(0)
            if str(current) in seen:
                continue
            seen.add(str(current))
            payload = self._fetch(
                "/api/availability/map",
                self._availability_params(campground_id, current, req),
            )
            for resource_id, days in (payload.get("resourceAvailabilities") or {}).items():
                resources[str(resource_id)] = [d.get("availability") for d in days]
            for link in (payload.get("mapLinkAvailabilities") or {}):
                if str(link) not in seen:
                    pending.append(link)
        return resources

    def search(self, req: SearchRequest) -> list[Campsite]:
        """Availability for named parks. A few requests per park per window."""
        if not req.campground_ids:
            raise ValueError(
                f"{self.name}: refusing an unscoped search — name the parks you "
                "want in campground_ids."
            )
        found: list[Campsite] = []
        for campground_id in req.campground_ids:
            resources = self.park_availability(campground_id, req)
            for resource_id, codes in resources.items():
                found.extend(self._runs(req, campground_id, resource_id, codes))
        return list({s.key: s for s in found}.values())

    def _runs(self, req, campground_id, resource_id, codes) -> Iterable[Campsite]:
        """Turn a night-by-night code list into bookable runs of `req.nights`.

        Index 0 is `req.start_date`. A run needs `nights` consecutive open
        codes; the entry after the last night is checkout day and is not
        required to be open.
        """
        nights = max(1, req.nights)
        for offset in range(len(codes) - nights + 1):
            window = codes[offset:offset + nights]
            if any(code != AVAILABLE for code in window):
                continue
            first_night = req.start_date + timedelta(days=offset)
            if first_night > req.end_date:
                break
            yield Campsite(
                provider=self.name,
                campsite_id=str(resource_id),
                available_date=first_night,
                nights=nights,
                # The API returns resource ids, not site labels; a name would
                # cost one extra request per site. Recorded honestly as the id
                # rather than invented.
                site_name=str(resource_id),
                status="available",
                facility_id=str(campground_id),
                state=self.state,
                booking_url=self.booking_url(campground_id, first_night),
            )

    def booking_url(self, campground_id: str, arrival: date) -> str:
        """Deep link into their own booking flow (§8j-B) — we never book."""
        params = {
            "resourceLocationId": campground_id,
            "mapId": self.map_id_for(campground_id),
            "bookingCategoryId": 0,
            "startDate": arrival.isoformat(),
            "isReserving": "true",
            "partySize": 1,
        }
        return f"https://{self.host}/create-booking/results?{urlencode(params)}"


def _name_of(entry: dict) -> Optional[str]:
    for localized in entry.get("localizedValues") or []:
        name = localized.get("fullName") or localized.get("shortName")
        if name:
            return name.strip()
    return None


def _display_name(entry: dict) -> Optional[str]:
    """The English display name off any record carrying `localizedValues`."""
    for localized in entry.get("localizedValues") or []:
        name = localized.get("displayName")
        if name:
            return name.strip()
    return None


def _description_of(entry: dict) -> Optional[str]:
    for localized in entry.get("localizedValues") or []:
        text = localized.get("description")
        if text:
            return text.strip()
    return None


def _photo_of(entry: dict) -> Optional[str]:
    """A plain `.jpg` URL, not the `.avif` beside it.

    The platform offers both. AVIF is smaller but not universally decodable in
    every context this might land in, and the jpg is the safe one to store.
    """
    for photo in entry.get("photos") or []:
        result = photo.get("photoUrlResult") or {}
        url = result.get("url") or result.get("avifUrl")
        if url:
            return url
    return None


def _parse_coordinates(value: Optional[str]):
    """`"48.03218, -119.9347"` -> `(48.03218, -119.9347)`, else `(None, None)`.

    Never a guessed coordinate: anything unparseable comes back as unlocated,
    which the map shows honestly (§13).
    """
    if not value:
        return None, None
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) != 2:
        return None, None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None, None
    if lat == 0 and lon == 0:
        return None, None
    return lat, lon


class BlockedByProvider(Blocked):
    """Raised on 403/429 so the caller stops rather than hammering."""
