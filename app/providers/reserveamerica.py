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
_MATRIX_CELL = re.compile(
    r"class='td status a'><a href='[^']*siteId=(\d+)[^']*"
    r"arvdate=(\d{1,2}/\d{1,2}/\d{4})'[^>]*aria-label='A for ([^']+?) on "
)
_SITE_ROW = re.compile(r"<div class='br'>(.*?)(?=<div class='br'>|<div class='tfoot'|\Z)", re.S)
_SITE_ID = re.compile(r"changeSelectedSiteOL\((\d+)\)")
_SITE_TYPE_ICON = re.compile(r"images/type_(\w+)\.gif")
_SITE_NAME = re.compile(r"campsiteDetails\.do[^>]*>([^<]{1,40})<")
_SITE_TYPE_LABEL = re.compile(r"<div class='td'>\s*([A-Z][A-Z /-]{3,30})\s*</div>")


def _clean(value: Optional[str]) -> Optional[str]:
    return html.unescape(value).strip() if value else None


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
    """GET a URL: httpx, else requests, else the standard library.

    This is one plain GET with no session state, so it does not need a
    particular client — but it does need a **tolerant** one. Measured against
    this host on 2026-07-28, fetching the same directory page:

    | client                | result                                  |
    |-----------------------|-----------------------------------------|
    | `requests` (urllib3)  | 176 KB, complete                        |
    | `urllib` (http.client)| 74 KB, cut off mid-`<head>`, every time  |

    The server sends `Transfer-Encoding: chunked` and ends the body without its
    terminating chunk. urllib3 hands back what arrived; `http.client` raises
    `IncompleteRead`, and the partial body it carries really is short. So the
    stdlib path is a last resort that is known to fail on ReserveAmerica —
    kept only so the module imports and runs somewhere with neither library.
    Truncation is returned, never hidden: `page_is_complete` upstairs decides
    whether to retry or refuse.
    """
    headers = {"User-Agent": USER_AGENT}

    try:
        import httpx
    except ImportError:
        pass
    else:
        response = httpx.get(url, headers=headers, follow_redirects=True, timeout=40.0)
        return response.status_code, response.text

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
        """Every site in one park, from its own page — RA's search can't be trusted.

        Returns raw dicts rather than `Campsite`, because this is inventory
        (what exists), not availability (what's open on a date).
        """
        page = self._fetch(
            "campgroundDetails.do",
            {"contractCode": self.contract_code, "parkId": park_id},
        )
        return self.parse_sites(page)

    @staticmethod
    def parse_sites(page: str) -> list[dict]:
        collapsed = re.sub(r"\s+", " ", page)
        sites = []
        for row in _SITE_ROW.findall(collapsed):
            site_id = _SITE_ID.search(row)
            if not site_id:
                continue
            icon = _SITE_TYPE_ICON.search(row)
            label = _SITE_TYPE_LABEL.search(row)
            sites.append(
                {
                    "site_id": site_id.group(1),
                    "name": _clean(_SITE_NAME.search(row).group(1))
                    if _SITE_NAME.search(row)
                    else None,
                    # Icon is the reliable signal; the text label is missing on
                    # some rows. Both are recorded — neither is guessed.
                    "site_type": icon.group(1) if icon else None,
                    "site_type_label": _clean(label.group(1)) if label else None,
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
