"""Park alerts — burn bans and closures, from the operator's own notice page.

    python3 scripts/manage.py refresh-alerts

Scott found the source on 2026-07-31 (docs/fire-restrictions.md):

    https://parks.wa.gov/about/news-announcements/alerts

Every Washington state park's alerts, server-rendered, on **one page**. No
JavaScript, no pagination, no API to reverse-engineer, no session. One request
covers the state, which is why this is a daily job rather than a scan.

## The two things this answers

**"May I light a fire?"** — the filter Scott asked for. Distinct from the
planned wildfire enricher: that says whether something is burning nearby, this
says whether *you* may light one, and a park with clean air can still be under
a total ban.

**"Is it even open?"** — and this is the bigger win. The same page carries
`Park is Completely Closed` and `Part of the Park is Closed`, and today we
would show Nisqually (closed for construction) and Sequim Bay (closed June to
September) as `unknown` and let somebody drive there.

## The rule that shapes the whole module

The page says, in its own words:

    "A Burn Ban is in effect at all Washington State Parks and Properties at
    all times. Most camping parks are at a Burn Ban Level 1 or higher
    year-round."

**So a park with no burn-ban row is not a park where fires are allowed.** It
is a park whose level we were not told. Reporting "no restrictions" from the
absence of a row would be inventing permission to light a fire in a dry
forest — the worst version of the mistake this project keeps guarding against
(§8g). Absence is recorded as `unknown`, and the interface must say
"restrictions not listed — check with the park", never "fires allowed".

## Levels, in the operator's own words

| level | what it permits |
|---|---|
| 1 | fires in designated fire pits and grills; propane and gas grills |
| 2 | wood fires only in designated fire pits; gas and propane fine; charcoal may be restricted |
| 3 | gas/propane stoves and fire pits only — no charcoal, no wood |
| 4 | no open flames of any type; internal RV stoves OK; no smoking |
| no fires at any time | year-round prohibition, not a fire-season measure |

That last one is a **standing rule**, not an emergency: many marine and
heritage parks carry it with dates from 2024 and 2025. Presenting it as a
current restriction alongside a July Level 3 would misrepresent both.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from . import store
from .util import iso

log = logging.getLogger(__name__)

ALERTS_URL = "https://parks.wa.gov/about/news-announcements/alerts"
ALERTS_HOST = "parks.wa.gov"
ALERTS_SOURCE = "parks.wa.gov alerts"

#: One park's accordion block. Non-greedy up to the next park, so a park with
#: several alerts keeps all of them and a park with none is still seen.
_PARK_BLOCK = re.compile(
    r'<div class="accordion" id="accordion-([a-z0-9-]+)">(.*?)(?=<div class="accordion" id="accordion-|\Z)',
    re.S,
)
#: The park's display name, from its own accordion button.
_PARK_NAME = re.compile(r'aria-controls="[^"]*">\s*(.*?)\s*</button>', re.S)
#: One alert within a park.
_ALERT = re.compile(r'<div class="alert-info">(.*?)</div>\s*</span>', re.S)
_HEADING = re.compile(r"<h3>(.*?)</h3>", re.S)
_POSTED = re.compile(r'<time datetime="([^"]+)"')
_BODY = re.compile(r"<p>(.*?)</p>", re.S)
_TAG = re.compile(r"<[^>]+>")

#: Alert headings that mean the campground may not be usable. Matched on the
#: heading text the page itself prints, not on an icon class.
CLOSURE_HEADINGS = ("Park is Completely Closed", "Park Completely Closed")
PARTIAL_HEADINGS = ("Part of the Park is Closed", "Part of Park Closed")

BURN_BAN = "Burn Ban"
LEVEL_UNKNOWN = "unknown"


@dataclass
class Alert:
    park_slug: str
    park_name: str
    alert_type: str
    #: For a burn ban: "1".."4" or "no fires at any time". None otherwise.
    level: Optional[str]
    posted: Optional[str]
    text: str

    @property
    def is_burn_ban(self) -> bool:
        return self.alert_type == BURN_BAN

    @property
    def closes_park(self) -> bool:
        return any(h.lower() in self.alert_type.lower() for h in CLOSURE_HEADINGS)

    @property
    def closes_part(self) -> bool:
        return any(h.lower() in self.alert_type.lower() for h in PARTIAL_HEADINGS)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", html)).strip()


def split_heading(heading: str) -> tuple[str, Optional[str]]:
    """`"Burn Ban  Level 3"` -> `("Burn Ban", "3")`.

    The page writes the type and the level in one `<h3>`, separated by a
    double space that is not reliable enough to split on. So the known types
    are matched as prefixes and whatever remains is the level.
    """
    text = re.sub(r"\s+", " ", heading).strip()
    if text.lower().startswith("burn ban"):
        rest = text[len("burn ban"):].strip()
        if not rest:
            return BURN_BAN, None
        m = re.match(r"level\s*(\d+)", rest, re.I)
        return BURN_BAN, (m.group(1) if m else rest.lower())
    return text, None


def parse_alerts(html: str) -> list[Alert]:
    """Every alert on the page, park by park.

    Parsed by walking the accordion blocks rather than hunting for headings:
    an alert has to belong to a park, and a regex that found alerts first
    would happily attribute one to whichever park name it saw last — the
    quiet kind of wrong that looks fine until somebody drives somewhere.
    """
    out: list[Alert] = []
    for slug, block in _PARK_BLOCK.findall(html):
        if slug.endswith("-heading") or slug.endswith("-body"):
            continue
        name_match = _PARK_NAME.search(block)
        name = _text(name_match.group(1)) if name_match else slug
        for alert_html in _ALERT.findall(block):
            heading = _HEADING.search(alert_html)
            if not heading:
                continue
            alert_type, level = split_heading(_text(heading.group(1)))
            posted = _POSTED.search(alert_html)
            body = _BODY.findall(alert_html)
            out.append(Alert(
                park_slug=slug,
                park_name=name,
                alert_type=alert_type,
                level=level,
                posted=posted.group(1) if posted else None,
                text=" ".join(_text(b) for b in body).strip(),
            ))
    return out


def fetch_alerts(fetcher=None) -> list[Alert]:
    """The live page, parsed. One request for the whole state."""
    if fetcher is not None:
        return parse_alerts(fetcher())
    import requests

    from .pacing import shared_limiter

    with shared_limiter().slot(ALERTS_HOST, label="WA park alerts"):
        response = requests.get(
            ALERTS_URL,
            headers={"User-Agent": "CampgroundFinder/0.1 "
                                   "(personal campsite availability tracker; low volume)"},
            timeout=45,
        )
    response.raise_for_status()
    return parse_alerts(response.text)


def normalize_park_name(name: str) -> str:
    """A key both sides of the match can agree on.

    The alerts page and the reservation platform do not spell parks the same
    way — "Alta Lake State Park" against "Alta Lake", "Mt. Spokane Sno-Park"
    against "Mount Spokane State Park". Strip the decoration both add and
    compare what's left.
    """
    text = html_unescape(name or "").lower()
    text = re.sub(r"\b(mt\.?)\b", "mount", text)
    text = re.sub(
        r"\b(state park|park|heritage site|historical|recreation area|"
        r"marine|sno-park|state)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


#: Names the two sides genuinely disagree on, mapped by hand.
#:
#: Deliberately a table and not a fuzzier matcher. The remaining mismatches
#: are cases where the *distinguishing* words differ — "Sun Lakes" against
#: "Sun Lakes-Dry Falls", "Hope Island (Mason)" against "Hope Island Marine
#: State Park - Mason County", where "Mason" is what separates it from the
#: Skagit County Hope Island. Loosening the matcher enough to catch those
#: would also merge parks that are actually different, and the cost of a wrong
#: match here is telling somebody they may light a fire when they may not.
#:
#: Keys and values are both `normalize_park_name` output.
PARK_ALIASES = {
    "hopeislandmasoncounty": "hopeislandmason",
    "sunlakesdryfalls": "sunlakes",
}


def strip_subarea(name: str) -> str:
    """Drop a catalog name's sub-area suffix: the bit after " - " or in "()".

    The reservation platform names *campgrounds*; the alerts page names
    *parks*. Riverside State Park posts one burn ban and holds two campgrounds
    — "Riverside State Park - Bowl and Pitcher" and "- Lake Spokane" — so the
    suffix has to come off or both miss.

    Used only as a **second pass**, never first, because the suffix sometimes
    carries the whole distinction: "Lewis and Clark State Park (SW Washington)"
    and "Lewis and Clark Trail State Park (SE Washington)" are different parks
    and a looser match would merge them.
    """
    text = re.sub(r"\s*\([^)]*\)", "", name or "")
    text = re.split(r"\s+[-–]\s+", text)[0]
    return text.strip()


def html_unescape(text: str) -> str:
    """`Doug&#039;s Beach` -> `Doug's Beach`. The page is full of these."""
    import html as _html

    return _html.unescape(text or "")


def match_to_catalog(
    conn: sqlite3.Connection,
    alerts: Iterable[Alert],
    provider: str = "GoingToCamp:WA",
) -> tuple[dict, list[str]]:
    """`({campground_id: [Alert]}, unmatched_park_names)`.

    **The unmatched list is returned, not swallowed.** A park whose name we
    failed to match is a park whose burn ban we are not showing, and that is
    exactly the silently-short shape this project keeps being bitten by. The
    caller prints it.
    """
    # Both indexes map to a LIST of campground ids, because one park can hold
    # several campgrounds: Riverside posts one burn ban and we carry its Bowl
    # and Pitcher and Lake Spokane camps separately. The ban applies to both,
    # and picking one would leave the other silently unwarned.
    exact: dict[str, list[str]] = {}
    stripped: dict[str, list[str]] = {}
    for cg in store.list_campgrounds(conn, provider=provider):
        exact.setdefault(normalize_park_name(cg.name), []).append(cg.id)
        stripped.setdefault(
            normalize_park_name(strip_subarea(cg.name)), []).append(cg.id)

    matched: dict[str, list[Alert]] = {}
    unmatched: list[str] = []
    for alert in alerts:
        key = normalize_park_name(alert.park_name)
        key = PARK_ALIASES.get(key, key)
        # Exact first, sub-area-stripped second. Never the other way round:
        # the suffix is sometimes the whole distinction between two parks.
        ids = exact.get(key) or stripped.get(key)
        if not ids:
            if alert.park_name not in unmatched:
                unmatched.append(alert.park_name)
            continue
        for cg_id in ids:
            matched.setdefault(cg_id, []).append(alert)
    return matched, unmatched


def refresh_alerts(
    conn: sqlite3.Connection,
    provider: str = "GoingToCamp:WA",
    fetcher=None,
    now: Optional[datetime] = None,
) -> dict:
    """Fetch, match, store. Returns a report including what didn't match."""
    alerts = fetch_alerts(fetcher=fetcher)
    matched, unmatched = match_to_catalog(conn, alerts, provider=provider)
    stamp = iso(now)

    store.replace_park_alerts(conn, provider, [
        (cg_id, a.alert_type, a.level, a.posted, a.text, stamp)
        for cg_id, park_alerts in matched.items()
        for a in park_alerts
    ])

    bans = sum(1 for a in alerts if a.is_burn_ban)
    closed = sum(1 for a in alerts if a.closes_park)
    log.info("WA alerts: %d alerts, %d burn bans, %d closures, %d parks matched, "
             "%d names unmatched", len(alerts), bans, closed, len(matched),
             len(unmatched))
    return {
        "alerts": len(alerts),
        "burn_bans": bans,
        "closures": closed,
        "parks_matched": len(matched),
        "unmatched": unmatched,
    }
