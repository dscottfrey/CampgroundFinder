"""ReserveAmerica (Aspira) provider — read-only, deliberately slow.

Parameterized by **(host, contract_code)**, because one platform serves many
agencies: Oregon is `oregonstateparks.reserveamerica.com` + `OR`, Georgia is
`a1.reserveamerica.com` + `GA`, and so on (see docs/reserveamerica-clients.md).

**Why this provider ignores ReserveAmerica's search entirely.** Reehers Camp
Horse Camp is bookable but invisible to RA search under *every* site type —
tent, any, and even horse, which it demonstrably has. Verified 2026-07-27: the
park page lists 20 horse sites and 14 tent sites, while no search surfaces it.
So discovery here walks the **full park directory** and reads each park
directly. Never add a search-based shortcut (§8k).

Verified live 2026-07-27 against Oregon:
  * `campgroundDirectoryList.do?contractCode=OR&startIdx=N` — the full park
    directory, 25 rows per page, with lat/lon embedded in each row. 65 parks.
    Reehers is present: parkId 412704.
  * `campgroundDetails.do?contractCode=OR&parkId=N` — one park's complete,
    unfiltered site list: site id, name, type, and attributes.

  * `campsiteDetails.do?contractCode=OR&parkId=N&siteId=M&arvdate=MM/DD/YYYY`
    — a **two-week availability grid for one site**. Day cells carry
    `data-auto-id='mdayYYYYMMDD'` and `class='td status a|x|r'`.

  * `campgroundDetails.do?contractCode=OR&parkId=N&arvdate=MM/DD/YYYY`
    — **the park-level matrix**: every site against 14 days, in ONE request.
    Only available cells are links; `x`/`r` cells are inert. This is 34x
    cheaper than walking sites individually, and it is what `search()` uses.

`campsiteCalendar.do` is a dead end — it redirects to the park page however it
is called. The park matrix above is the bulk route.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta
from typing import Iterable, Optional
from urllib.parse import urlencode

from ..pacing import Blocked, RateLimiter, shared_limiter
from .base import STATUS_UNKNOWN, Campground, Campsite, Provider, SearchRequest

log = logging.getLogger(__name__)

#: RA guards its traffic harder than any other source we use, and this runs
#: from a home connection that must not get blocked (§13). Slower than the 2s
#: we use for RIDB, on purpose. The authoritative copy of this number lives in
#: `pacing.HOST_DELAYS`, keyed by host — this is the documentation of it.
REQUEST_DELAY = 6.0
PAGE_SIZE = 25

#: Honest and descriptive. Never rotate or disguise this (§6c) — we are a
#: polite personal tool, not evading anyone.
USER_AGENT = "CampgroundFinder/0.1 (personal campsite availability tracker; low volume)"

#: Row in the directory listing: parkId, name, then lon:lat from the map link.
_DIRECTORY_ROW = re.compile(
    r"parkId=(\d+)'[^>]*>(?:<br>)?\s*([^<]{2,80})<br></a>.*?"
    r"switchViewType\([^)]*?&#39;(-?\d+\.\d+):(-?\d+\.\d+)&#39;",
    re.S,
)
#: One day cell in a site's two-week calendar. `a` available, `x` not
#: available, `r` reserved. The date rides along in data-auto-id.
_CALENDAR_CELL = re.compile(
    r"<div id='avail\d+' class='td status ([a-z]+)[^']*' title='([^']*)' "
    r"data-auto-id='mday(\d{8})'"
)
#: An available cell in the park-level matrix. Only available cells are links,
#: and each link carries its own arvdate — which is the only place the year
#: appears (the visible label is just "Aug 10").
#:
#: The `[^']*` after the date is load-bearing. RA appended `&lengthOfStay=1`
#: to these links between 2026-07-27 and 2026-07-28; the previous pattern
#: required the date to be followed immediately by the closing quote, so it
#: matched 150 cells in the captured fixture and **zero** live, while the page
#: plainly held 138 available cells. Every Oregon park silently read "full".
#: Anchor on what is stable, and never trust a zero from this parser without
#: checking the raw page for `td status a`.
_MATRIX_CELL = re.compile(
    r"class='td status a'><a href='[^']*siteId=(\d+)[^']*"
    r"arvdate=(\d{1,2}/\d{1,2}/\d{4})[^']*'[^>]*aria-label='A for ([^']+?) on "
)
_SITE_ROW = re.compile(r"<div class='br'>(.*?)(?=<div class='br'>|<div class='tfoot'|\Z)", re.S)
#: "279 site(s) found" — the park's own stated total, used to prove the walk
#: through its pages was complete.
_SITE_TOTAL = re.compile(r"matchSummary[^>]*>\s*(\d+) site\(s\) found")
_SITE_ID = re.compile(r"changeSelectedSiteOL\((\d+)\)")
#: The site id also rides in the last cell's class name.
_SITE_ID_FROM_CLASS = re.compile(r"sitescompareselectorbtn(\d+)")
#: One cell of the site table, in document order.
_ROW_CELL = re.compile(r"<div class='(td[^']*)'[^>]*>(.*?)(?=<div class='td|\Z)")
#: The row's type icon. **Do not treat this as access mode or equipment.**
#: Ground-truthed by Scott 2026-07-28: the sites in "Brooke Creek Hike-In Camp"
#: at L.L. Stub Stewart are named HIKE 01..HIKE 21, sit in a loop with
#: "Hike-In" in its name, cannot take an RV — and carry the `rv` icon. The site
#: name and loop name were right where the icon was wrong (docs/terminology.md).
_SITE_TYPE_ICON = re.compile(r"images/type_(\w+)\.gif")
_SITE_NAME = re.compile(r"campsiteDetails\.do[^>]*>([^<]{1,40})<")
#: An uppercase cell in the row. Despite the name this is often the **loop or
#: area name** ("DAIRY CREEK EAST", "BROOKE CREEK HIKE-IN CAMP") rather than a
#: site type ("TENT SITE"). Useful, but not a type — don't filter on it.
_SITE_TYPE_LABEL = re.compile(r"<div class='td'>\s*([A-Z][A-Z /-]{3,30})\s*</div>")


def _clean(value: Optional[str]) -> Optional[str]:
    return html.unescape(value).strip() if value else None


#: "20 Back-In" -> (20, "Back-In"); "Back-In" -> (None, "Back-In"); "" -> (None, None)
_DRIVEWAY = re.compile(r"^\s*(?:(\d+)\s*)?(.*?)\s*$")


def parse_driveway(value: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Split the Equip length/Driveway cell into (feet, manoeuvre)."""
    if not value or not value.strip():
        return None, None
    m = _DRIVEWAY.match(value)
    feet = int(m.group(1)) if m.group(1) else None
    manoeuvre = m.group(2) or None
    return feet, manoeuvre


def fits_equipment(driveway: Optional[str], length_needed: int):
    """Three-state: can a rig of `length_needed` feet use this site?

    **The listed length is a MINIMUM, not a measurement.** A Beverly Beach
    manager told Scott directly that when the park went onto ReserveAmerica
    they had no staffing to measure the sites, so most were entered at a
    default. Ground truth: A01 is listed `20 Back-In` and is really **53 feet**,
    while A15 — genuinely small — was entered accurately at 15. On that page 21
    of 24 sites read exactly "20 Back-In".

    So:

    * blank        -> **False**. No driveway at all means no vehicle reaches
      it; these are the WALK TO sites. The one case we can honestly exclude.
    * listed >= needed -> **True**. The floor already clears the requirement.
    * listed <  needed -> **None (unknown)**. The site may well be far longer,
      as A01 is. Never rendered as "no" — that would hide a site that fits,
      which is the failure this project exists to prevent (§8g).
    """
    feet, _manoeuvre = parse_driveway(driveway)
    if driveway is not None and not (driveway or "").strip():
        return False
    if feet is None:
        return None
    return True if feet >= length_needed else None


def page_is_complete(page: str) -> bool:
    """Did the listing finish rendering, or did the connection die partway?

    Verified live 2026-07-28: this host regularly ends a chunked response
    without its terminating chunk. Roughly half the directory requests came
    back cut off mid-`<head>` — HTTP 200, plausible HTML, and zero park rows,
    which is indistinguishable from the end of the directory.

    The signal is the **closing of the listing table**, not the closing of the
    document, because that is what survives being saved as a trimmed excerpt
    and is still absent from every truncated response we have seen. Measured
    against three real pages: the complete live page has it, the truncated live
    page does not, and the captured test fixture does.
    """
    tail = page.rstrip()
    return tail.endswith("</html>") or "</table>" in page


def _fetch_url(url: str) -> tuple[int, str]:
    """GET a URL with `requests`, falling back to the standard library.

    This is one plain GET with no session state, so it does not need a
    particular client — but it does need a **tolerant** one. Measured against
    this host on 2026-07-28, fetching the same directory page:

    | client                 | result                                 |
    |------------------------|----------------------------------------|
    | `requests` (urllib3)   | 176 KB, complete                       |
    | `urllib` (http.client) | 74 KB, cut off mid-`<head>`, every time |

    The server sends `Transfer-Encoding: chunked` and ends the body without its
    terminating chunk. urllib3 hands back what arrived; `http.client` raises
    `IncompleteRead`, and the partial body it carries really is short.

    **httpx is deliberately not used here**, though an earlier version tried it
    first. It is built on `h11`, a strict spec-correct parser, and this host's
    defining behaviour is a spec violation — so preferring it over a client
    proven to work on this exact page was risk with no upside. It has been
    dropped from requirements.txt rather than merely reordered.

    The stdlib path is a last resort known to fail on ReserveAmerica, kept only
    so the module runs somewhere without `requests`. Truncation is returned,
    never hidden: `page_is_complete` upstairs decides whether to retry or refuse.
    """
    headers = {"User-Agent": USER_AGENT}

    try:
        import requests
    except ImportError:
        pass
    else:
        response = requests.get(url, headers=headers, timeout=40)
        return response.status_code, response.text

    import urllib.error
    import urllib.request
    from http.client import IncompleteRead

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            try:
                raw = response.read()
            except IncompleteRead as exc:
                raw = exc.partial
            return response.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class ReserveAmericaProvider(Provider):
    #: `search()` refuses an unscoped crawl, so the scanner fills the scope in
    #: from the catalog — every park we know about, not a hand-picked few.
    requires_scope = True

    def __init__(
        self,
        contract_code: str,
        host: str,
        state: Optional[str] = None,
        delay: Optional[float] = None,
        fetcher=None,
        limiter: Optional[RateLimiter] = None,
    ):
        self.contract_code = contract_code
        self.host = host
        self.state = state or contract_code
        self.name = f"ReserveAmerica:{contract_code}"
        # Injectable so tests replay saved fixtures instead of hitting the site.
        self._fetch = fetcher or self._http_get
        # Pacing belongs to the process, not to this object — tier 1 and tier 2
        # must share one budget (docs/scanning-design.md). `delay` overrides it
        # with a private limiter, which is for tests replaying fixtures; real
        # runs leave it None and go through the shared one.
        if limiter is None:
            limiter = (
                shared_limiter()
                if delay is None
                else RateLimiter(delays={host: delay}, min_gap=delay, default_delay=delay)
            )
        self.limiter = limiter

    @property
    def delay(self) -> float:
        return self.limiter.delay_for(self.host)

    # -- transport ---------------------------------------------------------

    def _http_get(self, path: str, params: dict) -> str:
        url = f"https://{self.host}/{path}?{urlencode(params)}"
        with self.limiter.slot(self.host, label=self.name):
            status, text = _fetch_url(url)
        if status in (403, 429):
            # Back off hard and stop; never retry into a block (§13). Latching
            # it on the shared limiter stops every *other* caller too — an
            # on-demand refresh must not walk into a block the sweep just found.
            reason = f"{self.name} returned {status} — backing off"
            self.limiter.block(self.host, reason)
            raise BlockedByProvider(reason)
        if status >= 400:
            raise RuntimeError(f"{self.name}: HTTP {status} for {path}")
        return text

    # -- catalog -----------------------------------------------------------

    def list_campgrounds(
        self,
        state: Optional[str] = None,
        rec_area_ids: Optional[list[str]] = None,
    ) -> list[Campground]:
        """Walk the full park directory. Never a search, never a shortlist.

        The terminator is "a **complete** page yielded no new parks". The
        completeness half is not pedantry: verified live 2026-07-28, this host
        truncates roughly half its responses, and a page cut off mid-`<head>`
        parses to zero rows exactly like the end of the directory does. Without
        the check, enumeration stopped silently at 25 of 65 Oregon parks —
        alphabetically A through C, which drops Reehers and every park after
        it. That is the Reehers disappearance all over again, from a new cause,
        so a short directory now raises instead of being returned (§8k).
        """
        parks: dict[str, Campground] = {}
        offset = 0
        while offset < 1000:                       # sanity bound
            page = self._fetch_directory_page(offset)
            new = 0
            for park_id, name, lon, lat in _DIRECTORY_ROW.findall(page):
                if park_id in parks:
                    continue
                parks[park_id] = Campground(
                    provider=self.name,
                    id=park_id,
                    name=_clean(name) or park_id,
                    state=state or self.state,
                    latitude=float(lat),
                    longitude=float(lon),
                    reservation_type="reservable",
                    status=STATUS_UNKNOWN,
                )
                new += 1
            # The directory wraps around to page 1 once you run past the end,
            # so "no new parks" is the terminator — and it is only trustworthy
            # because the page it came from was checked for completeness first.
            if new == 0:
                break
            offset += PAGE_SIZE
        log.info("%s: %d parks in directory", self.name, len(parks))
        return list(parks.values())

    def _fetch_directory_page(self, offset: int) -> str:
        """One directory page, retried once if it arrives truncated.

        Checked on **every** page, not only on empty ones: a response cut off
        mid-listing yields some rows, which would advance the offset and skip
        the parks it never delivered — a silent hole in the middle of the
        catalog rather than a short tail.

        A truncated 200 is not a block signal — nobody asked us to stop — so
        one paced retry is fair. The rate limiter spaces it like any other
        request; never more than one retry, and never on a 403/429.
        """
        params = {"contractCode": self.contract_code, "startIdx": offset}
        page = self._fetch("campgroundDirectoryList.do", params)
        if page_is_complete(page):
            return page
        log.warning(
            "%s: truncated directory page at startIdx=%s — retrying once",
            self.name, offset,
        )
        page = self._fetch("campgroundDirectoryList.do", params)
        if page_is_complete(page):
            return page
        raise IncompleteDirectory(
            f"{self.name}: the directory page at startIdx={offset} arrived "
            f"truncated twice. Refusing to report a short directory as if it "
            f"were the whole thing (§8k)."
        )

    # -- per-park site inventory ------------------------------------------

    def list_sites(self, park_id: str) -> list[dict]:
        """Every site in one park — ALL pages — from its own page.

        The park page shows only the first 25 rows. Paging is **not** done by
        adding `startIdx` to `campgroundDetails.do`, which silently ignores it
        and returns page one forever; that made Beverly Beach look like a
        27-site park when it has 279, with its entire C loop invisible. The
        real control is a separate endpoint, taken from the page's own Next
        link:

            executePaging("/campsitePaging.do?contractCode=OR&parkId=N&startIdx=25")

        The walk is checked against the park's own "N site(s) found", because a
        short site list is the same silent failure as a short directory (§8k).

        Returns raw dicts rather than `Campsite`, because this is inventory
        (what exists), not availability (what's open on a date).
        """
        first = self._fetch(
            "campgroundDetails.do",
            {"contractCode": self.contract_code, "parkId": park_id},
        )
        stated = _SITE_TOTAL.search(first)
        expected = int(stated.group(1)) if stated else None

        sites: dict[str, dict] = {s["site_id"]: s for s in self.parse_sites(first)}
        offset = len(sites)
        while expected is None or len(sites) < expected:
            page = self._fetch(
                "campsitePaging.do",
                {
                    "contractCode": self.contract_code,
                    "parkId": park_id,
                    "startIdx": offset,
                },
            )
            new_rows = [s for s in self.parse_sites(page) if s["site_id"] not in sites]
            if not new_rows:
                break
            for row in new_rows:
                sites[row["site_id"]] = row
            offset += PAGE_SIZE
            if offset > 5000:                      # sanity bound
                break

        if expected is not None and len(sites) < expected:
            raise IncompleteSiteList(
                f"{self.name} park {park_id}: the page states {expected} sites "
                f"but only {len(sites)} were collected — refusing to report a "
                f"partial site list as the whole park."
            )
        log.info("%s: park %s has %d sites", self.name, park_id, len(sites))
        return list(sites.values())

    @staticmethod
    def parse_sites(page: str) -> list[dict]:
        """Parse the site table by COLUMN, not by pattern-hunting.

        The table is, in order:

            Site# | Loop | Site type | Max # of people | Equip length/Driveway
                  | Amenities | Online availability

        Reading it positionally matters. An earlier version grabbed the first
        uppercase cell it could find and labelled it `site_type_label`, which
        actually returned the **Loop** ("BROOKE CREEK HIKE-IN CAMP") and never
        the Site type. The Site type is where the access mode lives — for the
        Brooke Creek sites it reads **`WALK TO`**, which is the honest signal
        that you park elsewhere and carry your gear in.

        `equipment_length` empty is a second, independent signal for the same
        thing: a site no vehicle can reach has no driveway length. Both were
        confirmed against the live page by Scott, 2026-07-28.

        The type ICON is deliberately not used for this: those same WALK TO
        sites carry an `rv` icon and cannot take an RV (docs/terminology.md).
        """
        collapsed = re.sub(r"\s+", " ", page)
        sites: list[dict] = []
        seen: set[str] = set()
        for row in _SITE_ROW.findall(collapsed):
            site_id = _SITE_ID.search(row) or _SITE_ID_FROM_CLASS.search(row)
            if not site_id:
                continue
            site_id = site_id.group(1)
            if site_id in seen:
                # The page repeats each row (once rendered, once as a
                # template), which used to double every count.
                continue
            seen.add(site_id)

            cells = [
                _clean(re.sub(r"<[^>]+>", " ", inner)) or ""
                for _cls, inner in _ROW_CELL.findall(row)
            ]
            cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
            # Column 0 carries the map link's text before the site number.
            number = cells[0].replace("Map", "").strip() if cells else ""
            icon = _SITE_TYPE_ICON.search(row)
            sites.append(
                {
                    "site_id": site_id,
                    "name": number or None,
                    "loop": cells[1] if len(cells) > 1 else None,
                    # The real one: "WALK TO", "STANDARD", "TENT SITE", …
                    # NOT an equipment restriction — "TENT SITE" does not
                    # preclude a campervan, confirmed at Beverly Beach C27.
                    # Only an explicit "RV prohibited" in the site description
                    # says that, and we don't fetch descriptions yet.
                    "site_type": cells[2] if len(cells) > 2 else None,
                    "max_people": cells[3] if len(cells) > 3 else None,
                    # PRESENT vs EMPTY is meaningful — empty means no
                    # driveway, i.e. no vehicle reaches it. The NUMBER is not:
                    # Beverly Beach C27/C28 read "20 Back-In" and are really
                    # 40+ feet. Never use it as a hard bound (docs/terminology.md).
                    "equipment_length": cells[4] if len(cells) > 4 else None,
                    # Recorded but NOT to be trusted as access or equipment.
                    "type_icon": icon.group(1) if icon else None,
                }
            )
        return sites

    # -- availability ------------------------------------------------------

    @staticmethod
    def parse_calendar(page: str) -> list[tuple[date, str]]:
        """Extract (date, status) from a site's two-week calendar grid.

        Status is RA's own letter: `a` available, `x` not available,
        `r` reserved. We do not collapse x and r into "unavailable" — they
        mean different things and the distinction is worth keeping.
        """
        collapsed = re.sub(r"\s+", " ", page)
        out = []
        for status, _title, day in _CALENDAR_CELL.findall(collapsed):
            out.append(
                (date(int(day[:4]), int(day[4:6]), int(day[6:])), status)
            )
        return out

    def site_availability(self, park_id: str, site_id: str, arrival: date):
        """One site's two-week grid starting at `arrival`."""
        page = self._fetch(
            "campsiteDetails.do",
            {
                "contractCode": self.contract_code,
                "parkId": park_id,
                "siteId": site_id,
                "arvdate": arrival.strftime("%m/%d/%Y"),
            },
        )
        return self.parse_calendar(page)

    @staticmethod
    def parse_park_matrix(page: str) -> dict[tuple[str, str], set]:
        """All sites × 14 days from one park page. `{(site_id, name): {dates}}`.

        Only *available* cells carry a link; `x` and `r` cells are inert, so
        the presence of a link is itself the availability signal. Each link
        also carries its own `arvdate`, which is where the year comes from —
        the visible label is only "Aug 10".
        """
        collapsed = re.sub(r"\s+", " ", page)
        start = collapsed.find("id='daterangediv'")
        if start >= 0:
            collapsed = collapsed[start:]
        out: dict[tuple[str, str], set] = {}
        for site_id, arv, name in _MATRIX_CELL.findall(collapsed):
            month, day, year = (int(x) for x in arv.split("/"))
            out.setdefault((site_id, name.strip()), set()).add(date(year, month, day))

        # A zero that the page contradicts is a PARSER failure, not a full
        # campground — and "full" is the answer that quietly sends someone
        # elsewhere. RA changed this markup once already (adding
        # `&lengthOfStay=1`), turning every Oregon park into a silent "full"
        # while the page still advertised 138 open cells. Never report that
        # again without noticing.
        available_cells = collapsed.count("class='td status a'")
        if available_cells and not out:
            raise MatrixParseError(
                f"the page shows {available_cells} available cells but none "
                f"parsed — the markup has changed. Refusing to report this "
                f"park as full."
            )
        return out

    def park_availability(self, park_id: str, arrival: date):
        """One request → every site's availability for a fortnight."""
        page = self._fetch(
            "campgroundDetails.do",
            {
                "contractCode": self.contract_code,
                "parkId": park_id,
                "arvdate": arrival.strftime("%m/%d/%Y"),
            },
        )
        return self.parse_park_matrix(page)

    def search(self, req: SearchRequest) -> list[Campsite]:
        """Availability for named parks. One request per park per fortnight.

        Measured 2026-07-27: passing `arvdate` to `campgroundDetails.do` returns
        the **whole park matrix** — every site against 14 days — in a single
        response. Reehers came back as 16 sites and 150 available site-nights
        at once.

        That is 34x cheaper than the per-site page, and it makes a full Oregon
        sweep about 7 minutes rather than 4 hours. `campsiteCalendar.do` remains
        a dead end; it redirects however it is called.

        `campground_ids` is still required. Scanning every park in a contract is
        a decision the caller should make explicitly, not a default.
        """
        if not req.campground_ids:
            raise ValueError(
                f"{self.name}: refusing an unscoped search — name the parks you "
                "want in campground_ids."
            )

        found: list[Campsite] = []
        for park_id in req.campground_ids:
            window_start = req.start_date
            while window_start <= req.end_date:
                matrix = self.park_availability(park_id, window_start)
                for (site_id, name), days in matrix.items():
                    found.extend(self._runs(req, park_id, site_id, name, days))
                window_start += timedelta(days=14)
        # One site-night can appear in two overlapping fortnights.
        return list({s.key: s for s in found}.values())

    def _runs(self, req, park_id, site_id, name, days) -> Iterable[Campsite]:
        """Turn a set of open nights into bookable runs of `req.nights`."""
        for day in sorted(days):
            if day < req.start_date or day > req.end_date:
                continue
            window = [day + timedelta(days=n) for n in range(req.nights)]
            if not all(d in days for d in window):
                continue
            yield Campsite(
                provider=self.name,
                campsite_id=site_id,
                available_date=day,
                nights=req.nights,
                site_name=name or site_id,
                status="available",
                facility_id=park_id,
                state=self.state,
                booking_url=(
                    f"https://{self.host}/campsiteDetails.do"
                    f"?contractCode={self.contract_code}&parkId={park_id}"
                    f"&siteId={site_id}&arvdate={day.strftime('%m/%d/%Y')}"
                ),
            )


class IncompleteSiteList(RuntimeError):
    """A park's site list came back short of what the park itself claims."""


class MatrixParseError(RuntimeError):
    """The availability grid is present but unreadable.

    Raised instead of returning an empty result, because an empty result means
    "this park is full" to every caller downstream.
    """


class IncompleteDirectory(RuntimeError):
    """The directory walk could not be completed, so no list is returned.

    Raised instead of returning a short catalog. `catalog.refresh_catalog()`
    treats an enumeration error as "keep what we have" (§8k), which is the
    right outcome: a partial directory is worse than no update, because it
    looks like an answer.
    """


class BlockedByProvider(Blocked):
    """Raised on 403/429 so the caller stops rather than hammering.

    A `pacing.Blocked` subclass so the scanner catches one exception type for
    "this host has told us to stop", however it was discovered.
    """
