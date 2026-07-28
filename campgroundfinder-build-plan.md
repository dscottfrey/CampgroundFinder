# CampgroundFinder — Build Plan & Architecture Spec

A self-hosted, private replacement for CampSage: a browsable map + list of
available campsites near a home base, **plus** alerts when specific sites open
up. Runs as a local Docker stack, reachable only over your Tailnet. Data sources
are pluggable so you own the sources and the filtering and never depend on a
hosted app that might disappear or get enshittified.

> **How to use this doc:** it lives in the `CampgroundFinder/` working directory
> next to a **`samples/`** folder of reference projects (camply, the CampSage page
> source, the ReserveAmerica/PerfectMind scrapers — see §9 for the exact map).
> Point Claude Code here; it builds the app **at the top level of this directory**
> and treats `samples/` as read-only reference (`.gitignore` it — see §10). The
> doc carries the *verified* camply API surface (§12) so the adapter is built
> against reality, not guesses.

---

> ## ⚠ How to read the data in this document
>
> Every concrete value here — IDs, endpoints, field names, counts — was written
> **without running anything**, including sections labelled "VERIFIED against
> source". Treat each one as a **made-up example that is probably wrong** until
> it has been checked against a live source.
>
> Values confirmed live are marked **VERIFIED \<date\>**. Everything unmarked is
> still a guess. Corrected so far: camply's registry keys (§6, §12), the
> provider count (§4a), campground `232876` → **232831** (§8c), rec area `1106`
> (§8c, still unresolved), and `CampgroundFacility.coordinates` (§6).
>
> **Still unverified and load-bearing:** ReserveAmerica parkId **412704** for
> Reehers Camp Horse Camp (§8k) — the only real entry in the campground seed
> file, and the basis of the completeness acceptance test.

## 1. Goals

1. **Map + dashboard** — open a private URL, see what's actually available near
   your home base within a date window, filter it hard.
2. **Alerts** — set "watches" on specific campgrounds/date ranges and get pinged
   (phone/email/Slack/etc.) the moment a booked site frees up.
3. **Oregon + Washington checked by default, expandable** — the UI shows a
   selectable list of **regions (US states, Canadian provinces like BC, and
   national systems like Parks Canada)** with **only OR and WA enabled by
   default**. Check more to widen coverage; uncheck to narrow. recreation.gov
   spans the whole US and state/provincial/national systems layer on top, so
   scope is a user choice, not a hard limit — not California-locked like the
   blueprint repo, and explicitly built to cross into Canada.
4. **Pluggable sources** — camply handles the big providers; a thin custom
   provider handles anything camply doesn't (PerfectMind for San Juan County WA,
   and more later) — adding a source is one new class.
5. **Custom filtering** — distance, nights, weekends-only, equipment, site type,
   rating/reviews, loop, ADA, price — server- and client-side, easy to extend.
6. **Gated access, as wide as you choose (public repo, no anonymous view).** The
   **code repo is public** — it holds no secrets (all credentials are runtime-only,
   §13). Exposure of the running app scales to taste: **private** (Tailscale
   node-share with friends; identity via Tailscale, no passwords) → **wider but
   gated** (Tailscale Funnel or a public host with **app accounts you approve** —
   signups queue up and you approve them from an admin login). **No approved
   account → see nothing** (fine — nothing's for sale, so no anonymous teaser
   needed). **Multi-user** throughout: shared map, per-user private watchlists +
   alerts. Your data in your own SQLite. See §13.
7. **Air-quality gate (default on)** — a site is only shown/alerted if both
   current *and* forecast AQI are **green** (US AQI ≤ 50). Smoky openings are
   worthless in a PNW summer, so this is a first-class filter, not an
   afterthought. See §8d.
8. **Booking hand-off (+ optional account layer)** — the Book/check button
   **hands off** to the official site with site+dates pre-loaded (no in-app
   booking or payment); needs no credentials. Viewing past/upcoming trips is a
   **nice-to-have** — simplest as a zero-credential **manual trip log** ("Paige
   booked A13, Nov 23"), with optional account-sync only if wanted. See §8j.

**Build stance — don't over-automate.** Full automation always limits *something*,
so "last-mile" manual touches are **acceptable and preferred** over brittle
automation that strains to cover every edge: the right-click add (§8k), the manual
trip log (§8j), a hand-seeded catalog (§8k), supplying a one-off credential (§6b).
Automate the reliable 95%; leave a clean manual hook for the rest rather than a
fragile 100%.

## 2. Why this shape (the anti-enshittification part)

The data-fetching engine is **[camply](https://github.com/juftin/camply)** — an
MIT-licensed, actively maintained, community project that already speaks
recreation.gov, UseDirect (many state systems), GoingToCamp, and Yellowstone,
and has a built-in notification layer. You are not betting on one person's SaaS
staying up; you're running open-source code you control. Where camply doesn't
reach (e.g. PerfectMind county systems), you add a small read-only provider of
your own. The app is yours end to end; the only external things are the
government reservation APIs themselves.

## 3. Architecture

```
                    Tailscale (private URL, HTTPS via `tailscale serve`)
                                     │
                          ┌──────────▼───────────┐
   Browser (phone/laptop) │   FastAPI web app    │   ── serves Leaflet UI + JSON API
                          │  app/web.py          │
                          └──────────┬───────────┘
                                     │ reads/writes
                              ┌──────▼──────┐
                              │  SQLite     │  data/campgroundfinder.db
                              │ app/store.py│  (availability, watches, notifications)
                              └──────▲──────┘
                                     │ upserts
                    ┌────────────────┴───────────────────┐
                    │   Scanner (APScheduler job)         │  app/scanner.py
                    │   runs every N minutes              │
                    └───────┬───────────────────┬─────────┘
                            │ search()          │ on new match for a Watch
                    ┌───────▼────────┐   ┌───────▼────────┐
                    │  Providers     │   │   Notifier     │  app/notifier.py
                    │  (pluggable)   │   │  (Apprise)     │  → ntfy/Telegram/email/Slack…
                    └───────┬────────┘   └────────────────┘
          ┌─────────────────┼───────────────────────────┐
   ┌──────▼──────┐   ┌───────▼────────┐         ┌────────▼─────────┐
   │ CamplyProv. │   │ CamplyProv.    │  …       │ PerfectMindProv. │
   │ recreation  │   │ OregonMetro /  │          │ San Juan Co. WA  │
   │ .gov        │   │ any of 21      │          │ (custom scraper) │
   └─────────────┘   └────────────────┘          └──────────────────┘
```

One container runs the web server **and** the scheduler in-process (simplest
reliable design for a personal stack — no cross-container SQLite sharing). An
optional Tailscale sidecar container publishes it to your Tailnet.

## 4. Data sources

> **Inclusion rule (the one test for any source).** This is a reservation-based
> availability app: a source qualifies **only if it exposes an availability
> signal** — either **reservation availability** (open vs. booked) *or* a
> **first-come-first-serve occupancy/availability status**. FCFS sites have no
> reservation, but if the source reports whether they're open/full, they're in
> (flagged `first_come`, shown with status but **no booking link** — you just
> show up). A source with *no* availability signal of either kind is out — that's
> why mooring buoys / static "here's where things are" layers don't belong here.

### 4a. Via camply (free, no reverse-engineering)
> **VERIFIED 2026-07-27** against installed camply **0.34.2**: the registry
> holds **19** provider classes, not 21. The list below is otherwise correct.

camply exposes 19 provider classes. Verified identifiers (the `--provider`
strings / search-class names):

| Region | Provider string |
|---|---|
| US Federal (nationwide) | `RecreationDotGov` (+ `RecreationDotGovTicket`, `RecreationDotGovTimedEntry`, daily variants) |
| Yellowstone lodges/campgrounds | `Yellowstone` |
| WA state parks + others / Canada | `GoingToCamp` |
| California state parks | `ReserveCalifornia` |
| State parks (UseDirect) | `AlabamaStateParks`, `ArizonaStateParks`, `FloridaStateParks`, `MinnesotaStateParks`, `MissouriStateParks`, `OhioStateParks`, `VirginiaStateParks` |
| Regional / county | `OregonMetro`, `FairfaxCountyParks`, `MaricopaCountyParks` |
| International | `NorthernTerritory` (Australia) |

"Everything camply supports" = wire all of these into config as sources; enable
the ones you care about.

### 4b. Custom providers (what camply can't reach)
**PerfectMind / BookMe4** powers many county & municipal park systems, including
**San Juan County WA**
(`sanjuancountywa.perfectmind.com/.../BookMe4?widgetId=d69eb041-…`). camply has
no PerfectMind provider, so this is the flagship example of the pluggable layer.
See §9 for how to build it using your two reference repos.

### 4c. Adding more later
Any new source = subclass `Provider`, implement `search()`, register it. The rest
of the system (storage, map, filters, watches, alerts) is source-agnostic.

### 4d. More sources you specifically want (verify platform locally)
- **BC Provincial Parks** (`camping.bcparks.ca`): reservations run on the
  **GoingToCamp** platform, which camply already speaks — so this is *most
  likely a camply `GoingToCamp` config entry, not a custom scraper*. Confirm
  against the local camply clone that a BC agency/rec-area is exposed in its
  GoingToCamp list; if it is, add it like any other source. If BC Parks isn't
  pre-wired, camply's GoingToCamp client can usually be pointed at the site's own
  endpoints with a small subclass rather than a from-scratch provider.
- **Parks Canada national parks** (`reservation.pc.gc.ca`): not in camply →
  custom provider, same read-only pattern as PerfectMind (§7). Availability is
  served by JSON XHR behind the booking site; inspect and replay the *read*
  calls. Unofficial API write-ups exist (e.g. a Parse.bot marketplace listing)
  as a reference for endpoint shape — use them to learn the shape, don't depend
  on a third party at runtime.
- **Campspot** (`campspot.com`): private-campground platform, not in camply →
  custom provider. Campspot's official booking API is partner/B2B-only, but each
  park's public booking page makes JSON availability calls you can replay for the
  **specific** park. Config it per-park (park slug/id + a hand-filled lat/lon),
  read-only, same skeleton as §7. Start with just the one private camp you've
  used, then generalize.
- **Oregon State Parks (and many other states) via ReserveAmerica**
  (`oregonstateparks.reserveamerica.com`, `contractCode=OR`): a large legacy
  platform (Aspira) that camply does **not** support → custom provider. Worth
  **prioritizing**, because it's the **richest attribute source** you have —
  per-site it exposes `Tent Pad Length` *and* `Tent Pad Width`, `Max Vehicle
  Length` (≈ driveway size), and electric/water/sewer hookups (screenshot-
  confirmed: Ainsworth B16 = 12×12 pad, 45' max, 50A full hookups). Scraping is
  fiddlier than the JSON providers (ASP.NET session cookies + viewstate;
  endpoints like `unifSearch.do`, `campsiteCalendar.do`, and the per-site detail
  call) and ReserveAmerica guards its traffic — go gentle (slow interval,
  realistic headers, cache hard). This is the source that makes the tent-pad and
  vehicle-length filters **hard** rather than soft (§8f), and it's the same
  platform behind many other states' parks, so the one provider unlocks a lot.
  **RA query gotcha — CORRECTED 2026-07-27 (observed firsthand by Scott on the
  live site; not yet re-tested in code).** RA's search does not merely mis-handle
  "any" — at Reehers it is **totally broken**. Reehers has both horse sites and
  tent sites, and:
  - searching **tent** sites → Reehers does not appear
  - searching **any** sites → Reehers does not appear
  - searching **horse** sites → Reehers *still* does not appear
  - but selecting **Reehers from the master park list** → you can book a tent site

  So **no site-type query of any kind surfaces this park**, even the type it
  definitely has. The earlier claim in this section — that "horse returns sites
  even when any doesn't", so you can **enumerate every site type and union the
  results** — is **wrong, and that mitigation does not work.** Unioning zero
  results still yields zero.

  **The only reliable method:** ignore RA's search entirely. Walk the **full park
  directory** (`campgroundDirectoryList` for a `contractCode`), then query **each
  park's own availability** directly by parkId, and classify site types from what
  comes back. This is the §8k catalog model, and Reehers is the proof that it is
  mandatory for RA rather than a nicety — a park can be **100% invisible to
  search while being fully bookable.**
These sources are exactly why the region selector and provider registry are
generalized beyond US states — the model already handles provinces and national
systems, so each is one `Provider` (or camply config entry) + a region tag.

## 5. The provider interface (the core extensibility point)

Everything normalizes to one internal model, so the map/filters/alerts never care
where a site came from.

```python
# app/providers/base.py
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
    reservation_type: str = "reservable"  # 'reservable' | 'first_come' (FCFS: show status, no booking link — §4 inclusion rule)
    rec_area: Optional[str] = None
    rec_area_id: Optional[str] = None
    facility_name: Optional[str] = None
    facility_id: Optional[str] = None
    booking_url: Optional[str] = None
    state: Optional[str] = None        # region code: "OR", "WA", province "BC", or "CA-NAT" (Parks Canada) — drives the region selector
    aqi_status: Optional[str] = None   # 'green' | 'not_green' | 'tbd' | 'unknown' (set by the AQI enricher, §8d)
    fire_status: Optional[str] = None  # 'clear' | 'near' | 'unknown' (wildfire enricher, §8e)
    attributes: dict = field(default_factory=dict)  # normalized: max_vehicle_length, tent_pad_len, tent_pad_w, elec_amps, water, sewer, hookups… (null = unknown; §8f/§8g)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    extra: dict = field(default_factory=dict)   # provider-specific: rating, equipment, price…

    @property
    def key(self) -> str:              # stable identity for dedupe/notify
        return f"{self.provider}|{self.campsite_id}|{self.available_date}|{self.nights}"

class Provider(ABC):
    name: str
    @abstractmethod
    def search(self, req: SearchRequest) -> list[Campsite]:
        ...
```

## 6. camply adapter (VERIFIED against source — do not improvise)

```python
# app/providers/camply_provider.py
from datetime import date
from .base import Provider, SearchRequest, Campsite

class CamplyProvider(Provider):
    """Wraps one camply search class (e.g. SearchRecreationDotGov)."""
    def __init__(self, provider_name: str):
        # VERIFIED 2026-07-27 against camply 0.34.2: the registry is built as
        #   {provider.provider_class.__name__: provider for provider in ...}
        # (camply/search/__init__.py:57), so keys are PROVIDER names —
        # "RecreationDotGov", "GoingToCamp", … — NOT "SearchRecreationDotGov".
        # Confirmed live: 'RecreationDotGov' in registry -> True
        #                 'SearchRecreationDotGov' in registry -> False
        self.provider_name = provider_name
        self.name = provider_name

    def search(self, req: SearchRequest) -> list[Campsite]:
        # Lazy import so the app boots even if camply isn't installed yet.
        from camply.search import CAMPSITE_SEARCH_PROVIDER      # dict: "SearchX" -> class
        from camply.containers import SearchWindow

        search_cls = CAMPSITE_SEARCH_PROVIDER[self.provider_name]
        window = SearchWindow(start_date=req.start_date, end_date=req.end_date)

        finder = search_cls(
            search_window=window,
            recreation_area=req.rec_area_ids or None,   # provider subclasses accept these via **kwargs
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
        return [self._normalize(c) for c in found]

    def _normalize(self, c) -> Campsite:
        loc = getattr(c, "location", None)
        return Campsite(
            provider=self.name,
            campsite_id=str(c.campsite_id),
            available_date=c.booking_date.date() if hasattr(c.booking_date, "date") else c.booking_date,
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
            latitude=getattr(loc, "latitude", None) if loc else None,
            longitude=getattr(loc, "longitude", None) if loc else None,
            extra={"permitted_equipment": [getattr(e, "equipment_name", str(e))
                                           for e in (c.permitted_equipment or [])]},
        )
```

> **VERIFIED 2026-07-27 (live).** A real search through this adapter against
> Trillium (campground `232831`, Mt. Hood NF) returned 11 open site-nights with
> correct coordinates and booking links. Confirmed working: the
> `recreation_area` / `campgrounds` / `campsites` kwargs, and
> `get_matching_campsites(continuous=False, notification_provider="silent")`.
> Also confirmed: camply ships its own RIDB service-account key, so **no RIDB
> API key is needed**. And `CampgroundFacility.coordinates` is declared but
> **never populated by any provider** — directory enumeration yields no lat/lon,
> so seeded/RIDB coordinates are the only source.
>
> **BUILD NOTE — verify kwargs per provider.** `recreation_area` / `campgrounds`
> / `campsites` are correct for `RecreationDotGov`, but some UseDirect and
> GoingToCamp subclasses use different constructor keyword names. With the camply
> repo cloned locally (§9), confirm each provider subclass's `__init__` before
> wiring it up — don't trust this snippet blindly for non-federal providers. If
> the library ever fights you, the CLI path in §12 is the more stable contract.
> Stamp `Campsite.state` from the source's configured `state` (see config
> example below) — camply's availability payload doesn't reliably carry a state,
> so the source config is the source of truth for the state selector.

**Finding rec-area / campground IDs** (do this once when configuring a source):
```
camply recreation-areas --search "Mount Hood" --provider RecreationDotGov
camply campgrounds --rec-area 1106 --provider RecreationDotGov
camply campgrounds --search "state park" --state OR --provider OregonMetro
```

## 6b. When a provider needs credentials just to *read* availability

Most core sources (recreation.gov, ReserveAmerica, GoingToCamp, the camply
providers) read **anonymously** — camply fetches them with no login — so for the
PNW set this is largely a non-issue. But if a source needs an account even to
*see* availability, decide by **how often the credential is needed**:
- **One-time / static credential →** use a single **provider service account**
  (your own) stored server-side, encrypted with a key outside the DB (§13), used
  for all catalog/availability reads. Fine — it's one credential you control, not
  per-user custody.
- **Ongoing / refreshing auth** (expiring sessions, MFA, per-request tokens) →
  real maintenance and fragility. **Rethink including that provider**: degrade it
  to link-out + manual, or drop it, rather than babysitting a login.

This is separate from the optional per-user account linking (§8j — each friend's
*own* reservations); here it's the app's own read access to a source.

## 6c. Scraping architecture — internal JSON endpoints, caching, sessions

Cross-provider technical patterns (consolidates what's noted per provider):

**1. Hit internal JSON endpoints, not HTML/Selenium.** Every one of these
platforms populates its calendar grid from an **internal JSON availability
endpoint** — recreation.gov's month grid
(`/api/camps/availability/campground/{id}/month?start_date=…`), ReserveAmerica's
`campsiteCalendar.do` / `unifSearch.do`, PerfectMind's BookMe4 XHR. **Isolate
those via browser devtools → Network → XHR** and replay with `httpx`; never parse
the DOM or drive a headless browser (brittle, slow, blocks easily). camply already
does exactly this for recreation.gov + the UseDirect providers — a big reason to
lean on it rather than roll your own for those. (**Note: CampSage is _not_ built on
camply** — it queries these same public endpoints directly through its own backend.
That's equally valid; our plan is a **hybrid** — reuse camply's maintained wrappers
where they exist, and go direct-to-endpoint *like CampSage* for what camply lacks
(ReserveAmerica, PerfectMind). Same underlying endpoints either way; camply just
saves us re-implementing and maintaining the well-trodden ones. If camply ever
becomes a liability, each provider can be swapped to a direct implementation behind
the same `Provider` interface without touching the rest of the app.)

**2. Caching is architectural — our DB _is_ the cache.** The "add Redis so repeat
client requests don't duplicate upstream calls" advice is **already satisfied by
our shape**: only the **scheduled scanner** talks upstream; the browser/API reads
**our SQLite**, never the provider. So N friends refreshing the map = N cheap local
reads and **zero** extra upstream calls. Keep it that way — no client-triggered
upstream fetches, ever. Redis is optional hot-cache polish, unnecessary at personal
scale; SQLite + the scan cadence *is* the caching layer.

**3. Rate-limit + sessions, per provider.** Reinforcing §13: exponential backoff on
429/403, modest interval, stagger providers. **On the User-Agent I'd push back on
the research:** prefer an **honest, descriptive** UA — you're a polite personal
tool, not evading anyone. *Rotating/fake* UAs read as evasion, are the wrong
posture for a good citizen, and can escalate a block into a ban; only reconsider if
a provider blocks honest low-volume traffic, and then rethink including it (§6b).
**Sessions:** prefer **stateless** grids where they exist — recreation.gov's month
endpoint needs only `campground_id` + month, no auth/cookie, so it never expires.
For **stateful** ones (RA's ASP.NET session + viewstate), manage the cookie/
handshake lifecycle explicitly, **cache the session and reuse it across a scan**
(don't re-handshake per request), and **refresh on expiry** rather than letting a
stale session throw the errors that plague naive scrapers.

## 7. PerfectMind provider (San Juan County WA) — custom source

camply can't help here; PerfectMind BookMe4 has its own JSON backend that the
widget calls. Build a **read-only** provider that replays those calls to list
availability (no booking — you book manually in the browser).

> **BUILD SCOPE — READ THE AVAILABILITY HALF ONLY.** The reference repos in §9
> are full *booking* bots. Do **not** port their login, session-auth,
> captcha-solving, cart, or payment code. This provider does exactly one thing:
> fetch open slots and return them as `Campsite` records. You book manually in a
> browser. Replicating only the read path is dramatically simpler, less brittle,
> and keeps this respectful of the county's system. Ignore everything in those
> repos that isn't "list what's available."

```python
# app/providers/perfectmind.py
import httpx
from .base import Provider, SearchRequest, Campsite

class PerfectMindProvider(Provider):
    """
    Read-only availability for a PerfectMind BookMe4 widget.
    San Juan County WA: host=sanjuancountywa.perfectmind.com
                        widget_id=d69eb041-59af-41a7-9182-35e8487e05c1
    """
    def __init__(self, name: str, host: str, widget_id: str, calendar_ids: list[str] | None = None):
        self.name = name
        self.host = host
        self.widget_id = widget_id
        self.calendar_ids = calendar_ids or []

    def search(self, req: SearchRequest) -> list[Campsite]:
        # TODO (Claude Code, local): implement against the real BookMe4 endpoints.
        # Method:
        #  1. Open the widget URL in a browser with devtools → Network → XHR.
        #  2. Watch the JSON calls as you pick a date range. BookMe4 typically
        #     POSTs to paths under /{orgId}/Clients/BookMe4* returning JSON
        #     (calendar/service list, then per-day availability slots).
        #  3. Replicate those requests with httpx here; map each open slot to a
        #     Campsite(provider=self.name, campsite_id=..., available_date=...,
        #     nights=..., site_name=..., booking_url=<deep link>, lat/lon=<static
        #     per-campground table you fill in>).
        # Reference implementations that already talk to BookMe4 (see §9):
        #   - SeanXLChen/nvrc-perfectmind-booking
        #   - quantformity/MarkhamBooking
        raise NotImplementedError("Implement BookMe4 availability read — see docstring + §9 refs")
```

Notes: PerfectMind rarely exposes lat/lon, so keep a small hand-filled
`{campground_id: (lat, lon)}` table for map pins. This provider is read-only by
design — much simpler and less brittle than the booking bots you're referencing;
you only need the *availability* half of what they do.

## 8. Storage, scanning, filtering, alerts

**SQLite schema** (stdlib `sqlite3`, no ORM needed):
```sql
CREATE TABLE campgrounds (       -- the KNOWN UNIVERSE per provider (§8k). The map is drawn from THIS, not from search hits.
  provider TEXT, id TEXT,        -- provider-native campground/park id (e.g. ReserveAmerica parkId 412704)
  name TEXT, rec_area TEXT, state TEXT, latitude REAL, longitude REAL,
  reservation_type TEXT,         -- 'reservable' | 'first_come'
  status TEXT,                   -- 'available' | 'full' | 'closed' | 'unknown' | 'stale'
  status_reason TEXT, closed_until TEXT,   -- WHY it's unavailable (shown on click); reopen date if known
  first_cataloged TEXT, last_checked TEXT,
  PRIMARY KEY (provider, id)
);
CREATE TABLE availability (
  key TEXT PRIMARY KEY,          -- Campsite.key
  provider TEXT, campsite_id TEXT, available_date TEXT, nights INTEGER,
  site_name TEXT, loop TEXT, campsite_type TEXT, status TEXT,
  rec_area TEXT, rec_area_id TEXT, facility_name TEXT, facility_id TEXT,
  booking_url TEXT, latitude REAL, longitude REAL, extra TEXT,   -- extra = JSON (aqi detail + normalized attributes; null attr = unknown)
  aqi_status TEXT,               -- 'green'|'not_green'|'tbd'|'unknown' (denormalized for fast filtering, §8d)
  fire_status TEXT,              -- 'clear'|'near'|'unknown' (wildfire enricher, §8e)
  first_seen TEXT, last_seen TEXT
);
CREATE TABLE users (           -- Tailscale identity OR approved app account (§13)
  id INTEGER PRIMARY KEY, ts_login TEXT UNIQUE, email TEXT, name TEXT,
  role TEXT,                                                     -- 'admin' | 'friend'
  status TEXT,                                                   -- 'pending' | 'approved' (admin approves app-account signups)
  pw_hash TEXT,                                                  -- app-account mode (argon2/bcrypt); null for Tailscale-identity users
  notify_targets TEXT, created TEXT                              -- JSON: per-user Apprise URLs
);
CREATE TABLE watches (
  id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, provider TEXT,  -- user_id = owner (per-user watchlist)
  mode TEXT,                                                     -- 'targeted' | 'autonomous'
  rec_area_ids TEXT, campground_ids TEXT, campsite_ids TEXT,     -- JSON arrays
  start_date TEXT, end_date TEXT, nights INTEGER, weekends_only INTEGER,
  filters TEXT, notify_targets TEXT, active INTEGER, created TEXT -- JSON
);
CREATE TABLE notifications (
  id INTEGER PRIMARY KEY, watch_id INTEGER, campsite_key TEXT, sent_at TEXT
);
```

**Catalog refresh / reconciliation** (**very** slow cadence — **manual, or
monthly to semi-annually. NOT daily/weekly.** The set of campgrounds that exist
is close to static; re-scraping it often buys nothing and spends rate limit we
can't afford — see §13,
`app/catalog.py`): re-enumerate **every** campground per provider in enabled
regions and **diff against the stored catalog** — **add** new ones, **update**
changed ones, **mark closures** (`status='closed'` + `status_reason`,
`closed_until`), and **never delete**: an entry gone from enumeration for no clear
reason becomes `unknown`/`stale` and is flagged, not dropped (§8k). This is the
map's universe; keep it complete so nothing bookable is ever silently missing.

**Scan cycle** (`app/scanner.py`, every `scan_interval_minutes`):
1. For each catalogued campground in scope → verify availability live via the
   provider → `upsert_availability()` (updates `last_seen`; returns rows newly
   appeared) → stamp the campground's `status` (`available`/`full`/`unknown`/
   `stale`, §8k). The map is drawn from the **catalog** decorated with this
   status — not from search hits — so `full`/`unknown` campgrounds still show.
2. For each active **watch** → run its own search → for each matching site not
   already notified (`notifications` table) → `notifier.send()` →
   `record_notification()`.
3. Prune `availability` rows whose `last_seen` is older than the current window
   (they're gone/booked again) — but the campground stays in the catalog, dropping
   to `full`/`unknown`, never disappearing.

**Filtering model** — two layers, both easy to extend:
- *Server-side* (`GET /api/availability` query params): `green_only` (**default
  true** — AQI gate, §8d), `state` (repeatable; **defaults to OR,WA** from config
  when omitted), `provider`, `start`, `end`, `nights`, `max_miles` (haversine
  from home base), `campsite_type`, `rec_area`, free-text `q`, plus attribute /
  enrichment filters — `fire_clear`, `min_vehicle_length`, `tent_pad_min`,
  `hookups` — each returning **pass/fail/unknown** per site (§8g), not a boolean.
- *Client-side* (map UI): a **state selector** (checkbox list, OR + WA checked
  by default) plus live re-filter without refetch — toggle providers, drag a
  distance slider, filter by equipment/type, sort by distance or date.
- Add a new filter by adding one predicate — nothing else changes.

**Region selector behavior** (states / provinces / national systems): the
checkbox list is the primary scope control. Unchecking a region hides its sites
instantly (client-side filter on `Campsite.state`). Checking a region you
haven't configured a source for shows a one-line hint ("no source configured for
BC — add it to config.yaml") so the selector never silently returns nothing.
Enabling a new region for *scanning* = add a source block for it (below); the
default install ships OR + WA sources only, with BC / Parks Canada / Campspot
stubs commented and ready to enable.

**Distance**: haversine from `home_base (lat,lon)` in config to each site's
lat/lon; recreation.gov gives coordinates, PerfectMind uses your static table.

## 8b. Alerts: watchlists + autonomous watcher

An availability alert can fire two ways — both feed the same Notifier (§11) and
share the same triggering rules.

**1. Watchlist (targeted).** You name specific campgrounds / rec areas /
campsites + a date window + constraints (nights, weekends-only, equipment, max
distance). The scanner checks them every cycle; when a matching site is *newly*
available and hasn't already been alerted, you get pinged with a deep booking
link. The "I want **this** spot" case. Managed on the UI Watches page
(create / edit / pause / delete), stored in `watches` with `mode='targeted'`.

**2. Autonomous watcher (standing rule / bot).** Instead of naming a spot, you
define *criteria* and let it hunt across every configured source — e.g. "any
weekend, 2+ nights, within 120 mi of home base, in OR or WA, next 60 days,
tent-capable, rating ≥ 4." It runs on the same schedule, evaluates all fresh
availability against the rule, dedupes against what it already sent, and
notifies on anything new — **ranked** (nearest / best-reviewed first) and
optionally **batched** (one digest per cycle instead of a ping per site, so a
popular weekend opening doesn't spam you). The "surprise me with something good"
case. Same table, `mode='autonomous'`, campground/campsite IDs empty — it relies
on its filter block. Efficient by design: the scanner already fetches all
sources for the map, so the autonomous evaluator runs its predicate over that
same in-memory result set with **no extra API calls**; targeted watches do their
own focused search.

**Optional agent layer.** If you want a genuine "bot" rather than a rule engine,
add a thin, flag-gated step that hands each cycle's new-availability list (plus
the rule's intent in plain language) to an LLM to curate and write the copy
("3 riverside spots opened at Deschutes for Labor Day — closest is 42 mi"). The
scanning stays deterministic; the agent only decides *what's worth telling you
and how to phrase it*. Keep it off by default — the rule engine is cheaper,
faster, and works standalone.

**Triggering rules (both modes):**
- Fire only on **newly-appeared** availability (diff against `last_seen` /
  `notifications`), never every cycle.
- Dedupe on `Campsite.key`; add a re-notify cooldown (6–12 h) so a site that
  flaps in and out doesn't ping repeatedly.
- Every alert carries the deep **booking link** — popular sites rebook within
  minutes, so speed is the whole point.
- **Per-user, per-watch** notify targets: each friend owns their watches and
  sets where their own alerts go (their ntfy topic / Telegram / SMS), falling
  back to their `users.notify_targets` default. One person's watches never ping
  another's phone. The map/availability data is shared; watchlists and alerts are
  private per user (§13).

## 8c. Config example (`config.yaml`)

Ships with OR + WA sources only; add state blocks to widen. Rec-area IDs below
are **illustrative — look yours up** with `camply recreation-areas --search …
--state OR`.

```yaml
home_base:            { label: "Home", latitude: 45.52, longitude: -122.68 }
scan_interval_minutes: 30        # be polite; don't go below ~10
default_window_days: 60          # how far ahead the map + autonomous rules look
default_states: [OR, WA]         # state-selector defaults

notify:
  default_targets:
    - ntfy://ntfy.sh/change-me-campgroundfinder   # phone push; keep the topic secret

access:
  mode: tailscale             # identity from Tailscale Serve headers; no passwords (§13)
  admins: ["you@example.com"] # Tailscale logins treated as admin
  auto_provision_friends: true # first shared visitor becomes a 'friend' user

sources:                         # map/scan coverage; each tagged with a state
  # !! UNVERIFIED IDs below — 1106 did NOT resolve to Mt Hood on 2026-07-27.
  # A RIDB search for "Mount Hood" returns only 13113 (Lower White River
  # Wilderness), which has 0 campgrounds. Look each up before enabling.
  # Campground-level IDs that ARE verified live: Trillium = 232831.
  - { label: "Mt Hood NF",        provider: RecreationDotGov, state: OR, rec_area_ids: ["1106"] }
  - { label: "Deschutes NF",      provider: RecreationDotGov, state: OR, rec_area_ids: ["1113"] }
  - { label: "Gifford Pinchot NF",provider: RecreationDotGov, state: WA, rec_area_ids: ["1131"] }
  - { label: "WA GoingToCamp",    provider: GoingToCamp,      state: WA, rec_area_ids: [] }
  - label: "San Juan County (PerfectMind)"
    provider: "PerfectMind:SanJuanCoWA"
    state: WA
    perfectmind: { host: sanjuancountywa.perfectmind.com, widget_id: d69eb041-59af-41a7-9182-35e8487e05c1 }

  # --- Oregon State Parks (ReserveAmerica) — prioritize; richest attribute data (§4d/§8f) ---
  # - { label: "Oregon State Parks", provider: "ReserveAmerica:OR", state: OR, contract_code: OR, campground_ids: [] }
  #     # e.g. Ainsworth SP; exposes tent-pad L×W, max vehicle length, hookups → hard attribute filters

  # --- Canada / private: enable when their providers are built (see §4d) ---
  # - { label: "BC Provincial Parks", provider: GoingToCamp, state: BC, rec_area_ids: [] }
  #     # VERIFIED 2026-07-27: BC Parks IS in camply's GoingToCamp list as
  #     # "camping.bcparks.ca" — so this is a config entry, not a custom scraper.
  # - { label: "Parks Canada",        provider: "ParksCanada",         state: CA-NAT, rec_area_ids: [] }   # custom provider (§4d)
  # - label: "Campspot — <my park>"
  #   provider: "Campspot:<park-slug>"
  #   state: OR
  #   campspot: { park_slug: "<slug>", latitude: 0.0, longitude: 0.0 }   # custom provider (§4d)

watches:                         # optional seeds; or create them in the UI (stored in DB)
  - name: "Labor Day dream spot"
    mode: targeted
    provider: RecreationDotGov
    campground_ids: ["232831"]   # VERIFIED 2026-07-27: Trillium, Mt. Hood NF.
                                 # (Was 232876 — that ID does not exist.)
    start_date: 2026-09-04
    end_date: 2026-09-07
    nights: 2
    notify_targets: ["tgram://<bot_token>/<chat_id>"]
  - name: "Any good OR/WA weekend within 120mi"
    mode: autonomous
    states: [OR, WA]
    weekends_only: true
    nights: 2
    filters: { max_miles: 120, min_rating: 4.0, campsite_type_any: ["STANDARD NONELECTRIC", "TENT ONLY"] }
    batch: true                  # one ranked digest per cycle
    notify_targets: ["ntfy://ntfy.sh/change-me-campgroundfinder"]
```

## 8d. Air-quality gate (green-only) — the smoke filter

A first-class, **default-on** filter: a site's availability is shown/alerted
**only if both current and predicted AQI are "green" (US AQI ≤ 50, EPA "Good").**
Anything Moderate-yellow (51–100) or worse is excluded. In a PNW summer an open
site under wildfire smoke is worthless, so this gates **both the map and alerts**.

**Data source — Open-Meteo Air Quality API (primary):** free, **no API key**,
global (covers OR/WA *and* BC/Canada uniformly), returns `us_aqi` as both
`current` and `hourly` forecast, up to 7 days.
```
GET https://air-quality-api.open-meteo.com/v1/air-quality
      ?latitude=45.3&longitude=-121.7
      &current=us_aqi&hourly=us_aqi&forecast_days=7&domains=cams_global
```
It accepts comma-separated `latitude`/`longitude` lists, so a whole scan's unique
locations fetch in one or a few calls. **Optional upgrade — AirNow** (US EPA,
official categories, ~2-day forecast, free key, US-only): use inside the US for
authoritative categories, fall back to Open-Meteo elsewhere. Make the AQI source
pluggable, exactly like the campsite providers.

**Evaluation (per available site; needs lat/lon):**
1. Take the stay window (`booking_date` … `booking_end_date`).
2. **Within the forecast horizon (≤7 days out):** compute the *max* `us_aqi` over
   the hours covering the stay, plus current `us_aqi`. Green iff both ≤ 50 →
   `aqi_status='green'` (keep); otherwise `'not_green'` (exclude).
3. **Beyond the horizon (>7 days out):** AQI isn't predictable yet →
   `aqi_status='tbd'`. Do **not** hard-drop (that would blank the map — most
   availability is weeks out); keep it badged "AQI forecast pending," and let the
   autonomous watcher (§8b) re-check each cycle, only firing a green alert once
   the date enters the horizon and confirms green.
4. **No coordinates** (some PerfectMind/Campspot parks) → `aqi_status='unknown'`,
   badged as such.

**Strictness is config-driven** (`aqi:` block): `max_category` (default
`good`/green), plus what to do with `tbd` and `unknown` (default `show` badged;
set `hide` for a strict green-confirmed-only view).

**Caching & politeness:** round lat/lon to ~0.1° (≈11 km), cache the `us_aqi`
series per cell for 1–3 h (AQI moves slowly), refresh on the scan cycle. Persist
`aqi_status` on `availability` for fast SQL filtering and the detail in
`extra['aqi']` (`current`, `forecast_max`, `category`, `asof`, `source`).

```yaml
aqi:
  enabled: true
  provider: open-meteo        # keyless, covers Canada. Or "airnow" (needs AIRNOW_API_KEY, US-only)
  max_category: good          # green only; exclude Moderate(yellow) and worse
  forecast_days: 7
  on_tbd: show                # beyond forecast horizon: show badged | hide
  on_unknown: show            # no coordinates: show badged | hide
  cache_hours: 2
  grid_round_deg: 0.1
```

This same "enrichment" pattern (fetch external signal keyed on lat/lon → tag each
site → filter) is how you'd later add weather, fire-perimeter proximity, or
water-level layers. AQI is just the first enricher.

## 8e. More enrichers: wildfire proximity & water level

Both follow the AQI pattern (§8d): fetch an external signal keyed on the site's
lat/lon, tag the record, optionally gate on it. Both need coordinates; no coords
→ status `unknown` (badged, never silently dropped).

**Wildfire proximity (gate-capable).**
- *US:* **NIFC / WFIGS "Current Interagency Fire Perimeters"** — a public ArcGIS
  Feature Service (`data-nifc.opendata.arcgis.com`, queryable via ArcGIS REST →
  GeoJSON). Active fire *polygons*.
- *US + Canada uniformly:* **NASA FIRMS** active-fire hotspots (VIIRS/MODIS),
  near-real-time, global, free with a `MAP_KEY`. Point detections rather than
  perimeters — ideal for a simple "any fire within N miles" check across the
  border (covers your BC / Parks Canada sources with the same code).
- *Logic:* distance from the campsite to the nearest active perimeter (US) and/or
  hotspot (US+CA). `fire_status='near'` if within `radius_miles` (default ~25) →
  exclude or badge; else `'clear'`. Fetch the fire dataset **once per scan cycle**
  (it's national) and test all sites locally — no per-site API calls.
- *Canada perimeters (optional):* CWFIS (Canadian Wildland Fire Information
  System) for polygons north of the border; FIRMS hotspots already cover Canada
  for the proximity check.

**Water level (advisory, opt-in per campground).**
- *US:* **USGS Instantaneous Values** (`waterservices.usgs.gov/nwis/iv/`, JSON,
  **no key**) — real-time gage height (param `00065`) and discharge (`00060`).
  (USGS is migrating to the `api.waterdata.usgs.gov` OGC API; the legacy IV
  service still works — pick one and pin it.)
- *Canada:* Environment Canada hydrometric (`wateroffice.ec.gc.ca`) real-time.
- *Why opt-in:* "water level" means opposite things — flood risk on a river camp
  vs. a low reservoir killing the boat launch — so relevance is per-campground.
  Config maps a campground to a nearest **USGS site id** and what matters
  (`flood` or `low`). The enricher attaches current gage height/flow + a status
  (`normal`/`high`/`flood`/`low`) using NWS flood-stage thresholds where
  available, else a historical percentile. Default **advisory-only** (badge, no
  gate) — flaky water data shouldn't hide an otherwise-good site.

**Weather (badge; optional soft gate).** Same Open-Meteo family as AQI, keyless:
the forecast API gives temperature, conditions, and **precipitation probability**
per lat/lon (`api.open-meteo.com/v1/forecast?...&daily=precipitation_probability_max,
temperature_2m_max,weathercode`). CampSage shows exactly this ("63°F Sunny ·
☂ 1%"). Attach to each site for its stay dates as a one-line "conditions" badge
alongside AQI/fire; optionally soft-gate (flag/derank when rain probability >
threshold). Beyond ~7 days it's `tbd`, same as AQI.

**Cell coverage (filter; approximate).** You flagged this earlier — and the
CampSage page source shows *how they do it*: **recreation.gov's own
camper-reported cell-coverage ratings**, per carrier (Verizon/AT&T/T-Mobile),
`≥3/5 = good`. That's the **easy primary source** for federal sites — it rides
along with the availability data you're already pulling, no extra provider or
point-in-polygon math. For non-rec.gov sources (or broader coverage), fall back to
the **FCC National Broadband Map — Mobile** (`broadbandmap.fcc.gov`;
public BDC data API + downloadable per-carrier/technology coverage layers). Best
self-host pattern: download the mobile coverage polygons once for the
carriers/tech you care about and do **point-in-polygon locally** (no per-site
calls), tagging each site with modeled coverage (e.g. `verizon_4g`,
`tmobile_5g`). State the caveats plainly: it's **modeled**, not measured signal,
and Canada isn't in FCC data (use ISED coverage for BC/Canada, or crowdsourced
OpenCelliD / CellMapper tower data for real-world hints). So `min_signal` is a
`pass/fail/unknown` filter (§8g) like the rest — solid where data exists,
honestly `unknown` where it doesn't.

## 8f. Attribute filters (vehicle length, tent pad) — and the missing-data reality

These filter on **per-site attributes** the provider reports, normalized into
`Campsite.attributes`. The honest challenge (which you flagged): the data is
often absent and varies wildly by provider.

- **Vehicle / rig length — well-covered.** Reliable on **recreation.gov** (RIDB
  `Max Vehicle Length`, plus `Max Num of Vehicles`, `Driveway Length`,
  `Driveway Grade`, `Pad Type`, surfaced by camply via `campsite_attributes`)
  **and on ReserveAmerica / Oregon State Parks** (`Max Vehicle Length` ≈ driveway
  size — screenshot-confirmed 45' at Ainsworth B16). Filter `min_vehicle_length`
  = "site's Max Vehicle Length ≥ my rig." On providers that don't report it →
  `unknown`.
- **Tent pad size (e.g. 10×12 ft) — availability is provider-specific.** This is
  the key correction from the Ainsworth screenshot: it is **not** universally
  missing.
  - **ReserveAmerica / Oregon State Parks expose both `Tent Pad Length` and
    `Tent Pad Width`** as structured fields → here 10×12 is a **hard, exactly
    evaluable** filter (B16's 12×12 → pass; an 8'-wide pad → fail).
  - **recreation.gov** usually encodes at most a boolean `Tent Pad`, occasionally
    a pad *length*, almost never width×height → there it stays **soft** (flag /
    "verify at booking"), and absent data is `unknown`, never `fail`.
  Same three-state engine (§8g); the only difference is whether the source
  populated the dimensions. So: hard-filter where dims exist, soft-flag where
  they don't — and prioritize the ReserveAmerica provider precisely because it
  makes your friend's 10×12 requirement actually enforceable for OR state parks.
  (CampSage already scrapes ReserveAmerica for Oregon State Parks — e.g. Beverly
  Beach SP tagged "Oregon State Parks · ReserveAmerica" — so it's demonstrably
  feasible, and there's community-repo precedent to borrow from.)

**Missing-data policy (every attribute filter):** each carries `on_missing:
show_flagged | hide` (default `show_flagged`). A filter never silently drops a
site because the attribute is absent — it flags it "attribute unknown" so you
decide. This keeps sparse-data filters useful instead of blanking the map.

```yaml
# per-watch or global filter block
filters:
  min_vehicle_length: 28        # feet; site must fit a 28' rig (recreation.gov-reliable)
  tent_pad_min: [10, 12]        # feet [w,l]; HARD where dims exist (ReserveAmerica/OR State Parks), SOFT where only a boolean (recreation.gov)
  hookups: any                  # any | none | electric | water | sewer
  min_signal: { carrier: any, tech: 4g }   # cell coverage; pass/fail/unknown (§8g), often unknown
  on_missing: show_flagged      # show_flagged | hide — how to treat absent attribute data
wildfire:
  enabled: true
  radius_miles: 25
  source: firms                 # firms (US+CA, needs FIRMS_MAP_KEY) | wfigs (US perimeters)
  gate: exclude                 # exclude | badge
water:
  enabled: false                # advisory; opt in per campground below
  gauges:
    "232831": { usgs_site: "14211720", concern: flood }   # campground_id -> nearest gage
                                                          # (232831 verified; USGS gage id NOT verified)
weather:
  enabled: true
  provider: open-meteo          # temp, conditions, precip probability (keyless)
  rain_prob_gate: none          # none | flag | exclude  (soft by default)
cellcoverage:
  enabled: false                # modeled FCC mobile data; download layers, point-in-polygon locally
  carriers: [verizon, tmobile]  # min_signal is pass/fail/unknown (§8g); Canada needs ISED, not FCC
```

## 8g. Three-state filtering: pass / fail / unknown (never let "no data" look like "too small")

Every attribute and enrichment filter resolves each site to **one of three
states**, and the UI must keep them distinct:

- **pass** — data present, meets the requirement (site fits the 28' rig). Shown
  normally.
- **fail** — data present, does *not* meet it (Max Vehicle Length 20' < 28';
  tent pad 8' < 10'). A real "too small": hard filters hide it, soft filters
  demote/flag it.
- **unknown** — the attribute is absent for that site. **Never treated as fail,
  never auto-hidden.** Shown with a distinct neutral "? no data" badge.

The cardinal rule: **`unknown ≠ fail`.** A site with no vehicle-length field is
not "too small" — it's unmeasured, and hiding it loses real options (especially
on non-recreation.gov providers where the field is usually blank).

**How it shows up in the UI:**
- Each active filter renders a small state chip on every result card: green ✓
  (pass), red ✗ "too small" (fail), gray "?" (no data) — one glance tells you
  *why* a site does or doesn't match.
- The filter panel shows a live breakdown per filter so nothing hides silently:
  e.g. *"Fits 28' rig — 40 pass · 12 too small · 25 no-data."*
- Each filter exposes **two independent controls**, precisely so the two
  decisions never bleed together:
  1. **Strength** — `require` (hide fails) · `prefer` (show fails, ranked lower)
     · `ignore`.
  2. **Unknowns** — `show` (default) · `hide`. Separate axis, so "hide the
     too-small ones" never also hides the no-data ones unless you explicitly ask.
- Default everywhere: **require** on the fails you asked for, **show** on
  unknowns. So "I need 28 feet" hides genuinely-too-small sites but still
  surfaces the unmeasured ones with a "? verify length" badge.

**Data model:** store the normalized attribute *value* (or null) per site and
compute pass/fail/unknown at query time from value + threshold — don't collapse
to a boolean at scan time, or you lose the fail-vs-unknown distinction. The
denormalized status columns (`aqi_status`, `fire_status`) already use this
`green/not_green/tbd/unknown`-style multi-state; attribute filters follow suit
(value in `extra['attributes']`, null = unknown).

This supersedes the earlier `on_missing` shorthand: `on_missing: show_flagged` is
simply "unknowns = show," `hide` is "unknowns = hide" — Strength is the other,
separate axis.

## 8h. Map & result UI (your design)

The CampSage top bar is a good base; **the home page is the map** (a List view is
low value here — make it secondary or skip it). Keep the bar's search + filter
chips and add the date sliders in its empty left/center space.

**Rendering & tiles (this is _our_ frontend — camply is data only).** The map is a
standard web map *we* build; camply/providers only supply data (campgrounds,
availability, coords). Use **Leaflet** (simplest; raster tiles) to start, or
**MapLibre GL** (vector tiles — crisper, trail labels stay sharp at every zoom) as
an upgrade. **Tiles are a separate, swappable source**, and since you want **trail
names + outdoor detail**, skip the plain OSM road style. Good outdoor/topo options:
- **OpenTopoMap** (free, topographic, shows trails — light-use + attribution),
- **Thunderforest "Outdoors"** (API key; excellent trail/path detail),
- **USGS National Map** topo tiles (free, US),
- or layer a **Waymarked Trails** hiking overlay on any base for named routes.
Make the tile URL/key a **config value** so you can swap providers without code
changes. (Self-hosting tiles is possible but overkill for personal use.)

**Home = map + search.** Open straight to the map. A **search bar** finds a
campground / city / place by name for anything too small to spot on the map (jump
the map there). No separate landing page.

**Visual encoding (deliberate — one channel per meaning):**
- **Dot size = how many sites are available at that location**, on a
  **compressive curve** (radius ∝ √count or log, clamped to a max) so winter —
  when everything's open — doesn't blow the dots up into blobs.
- **Dot opacity = date-position × AQI** (multiply the two factors):
  - *Date fade:* within the visible date window, opacity fades by how far a
    location's availability sits across the display range — nearer dates solid,
    farther dates faded — as a % of the range (gross/quantized steps are fine).
  - *AQI fade (when the AQI layer is on):* **green = 100%**, **yellow = 50%**,
    **worse = 10%.** A smoky spot literally dims out; a clean near-term opening is
    boldest.
- **Color = reserved.** Black = the base catalog icon; the availability **overlay**
  dot is a single accent that just means "has filtered availability." Hue is
  deliberately **not** used to encode a variable (not provider, not nights) — left
  as a free channel for a future meaning (TBD) rather than spent now.
- **"Just opened"** gets a **non-color** cue (ring/glow) so it doesn't consume the
  reserved color channel.
- Cluster at low zoom; order closest-first to home base.
- **Two literal layers (the core rendering model).**
  - **Base layer — every known campground, always:** draw all catalogued
    campgrounds (§8k) as a **named black icon**. The whole universe is on the map
    at all times, independent of filters — the "nothing is ever missing" guarantee
    made literal.
  - **Overlay — filtered availability as a colored dot:** on a campground that has
    availability matching the **current filters + date window**, render the
    availability dot on top of its black icon, using the **size = count / opacity =
    date × AQI** encoding above. No matching availability → just the black icon, no
    dot. Either way the pin is clickable and explains itself (full / closed /
    no-data / out-of-window / filtered).
- **Tiles may already _label_ campgrounds — but that's just paint.** Outdoor tiles
  (OpenTopoMap etc., OSM-based) render campground names/icons into the basemap —
  nice free context, but **non-interactive pixels**. Our **catalog markers stay the
  interactive layer** (clickable for why + carrying availability); we don't rely on
  tiles for completeness or clicks. A campground painted on the tile but **not in
  our catalog** is a **gap signal** → the right-click add tool (§8k) turns it into
  a catalog entry.

**Date slider (in the top bar's open space) — a dual-handle range over
availability dates.** Track runs **Now → max date** (max configurable, **default
+1 month**). Two handles set the visible window:
- **Left handle = Now** by default; drag it right to **mask current/near dates**.
- **Right handle** defaults to **+15 days**; drag it to **mask far dates** (out to
  the configurable max).
Outside the window = hidden; inside it, the date-fade opacity above applies across
the window. Default view = anything available from now through ~15 days, near
dates boldest.

**Header & chrome.**
- Persistent **result count** ("547 shown"). The **map is the home**; a List view
  is optional/secondary (low value here — skip it if it's not worth building).
- **Freshness indicator** ("Updated 6h ago"); since ours is always-on, show the
  real last-scan time and a next-scan countdown.
- **Stats strip**: "Open now · Regions · Freed up recently" (985 / 20 / 216).
- **Filter bar** chips (CampSage set + ours): Just-opened, Dates, category
  (All/Camp/Beach), **how-many-nights** (Any/2+/3-night/Weekends), **adjacent
  sites** ("2+ sites together", §8i), Sought-after — plus region selector,
  green-only (AQI), fire-clear, hookups, min vehicle length, tent-pad size, min
  cell signal. The date **range** lives in the slider above, not just the Dates
  chip.
- **Nav**: My watchlist, Map, Camping by area, Camping by type, All campgrounds,
  Popular / Most-wanted, Cancellation report, Why/About.

**Site popup.** Availability **grouped by consecutive-night window**, each row
listing actual site IDs + "+N more" ("Aug 7–Aug 9 · 2nt → A05, A32, B05, C13
+78"); a **conditions line** (temp · precip% · AQI · fire · cell); actions:
**Book/check**, **Alert** (one-click watch on this park+dates), copy-link, Maps,
and a **full-details** expander (reviews, cell coverage, all dates). For
`first_come` sites there's **no Book/check** — show a "First-come — no
reservation" badge with the current status and Maps/directions instead.

**Click any pin to learn *why* — the #1 CampSage annoyance, fixed.** A greyed pin
opens a popup that states the reason plainly instead of leaving you guessing:
"Full — set alert," "Closed for season (reopens May 1)," "No availability data —
source has no feed / not checked yet," "Outside your date window — widen the
slider," or "Filtered out: tent pad < 10×12." Every pin explains itself; there is
never a mystery grey dot.

**Capture & share.** Inline **alert capture** and a **Share view** that encodes
current filters + map bounds in the URL.

**Deliberately dropped (the anti-enshittification dividend).** CampSage gates its
best behavior behind **Pro** (instant texts, unlimited watches) and pushes
affiliate links ("Rent a campervan", "Camping gear") and upsell modals ("Free vs
Premium — instant texts + unlimited watches"). Yours is self-hosted, so
**everyone gets the "Pro" behavior by default**: instant push (ntfy/Telegram/
SMS), unlimited watches, no tiers, no affiliate nags, and no "free map refreshes
~5×/day" throttle — you own the scan interval. That's exactly why you're building
this.

## 8i. Higher-value features worth borrowing (all computable from your own data)

- **Group-ready / adjacency detection.** Beyond "N sites open for the same
  dates," detect when those sites are **physically adjacent** (same loop +
  consecutive site numbers, or within X meters by coordinates) — what makes group
  camping actually work. Group availability by (park, date-window); within each,
  cluster by loop + numeric proximity; expose a `group_size` filter ("≥4 open
  together") and an `adjacent` toggle. Three-state again: adjacent / not /
  unknown (exact with coords, best-effort with just loop+number).
- **Sought-after / demand ranking ("Most wanted").** A per-park scarcity score
  from your **own** scan history: how fast openings get re-booked (lifetime of an
  availability row between `first_seen` and disappearance) and/or how rarely a
  park has anything. Drives a "Most wanted" leaderboard + the "Sought-after"
  chip, and can raise scan frequency / alert urgency for scarce parks. No
  external data.
- **"Freed up recently" feed (cancellation report).** You already compute
  newly-appeared availability each scan (§8b) — surface it as a reverse-chron
  feed and map ring ("216 freed up recently"), plus a light analytics page (which
  parks cancel most; what days/times openings tend to appear — useful for timing
  your own checks).
- **Ranked closest-first everywhere.** Default sort by home-base distance (you
  have haversine already); makes map and list instantly useful.

## 8j. Account integration (per-user): your reservations + booking hand-off

Two pieces with very different risk **and priority** — keep them separate. **The
booking hand-off (B) is the piece you want, and it's low-risk (no credentials).
The reservation view (A) is a nice-to-have you can skip entirely** — build it only
if/when you feel like it; the app is complete without it.

**A. Reservation view — "My trips." — NICE-TO-HAVE, not required.** Show each
friend's past + upcoming trips on the map + a "My trips" list, usable to enrich
alerts ("you already have Beverly Beach that weekend"). Two ways to populate it —
**do the simple one first; it may be all you need.**

*A1 — Manual trip log (zero credentials, the recommended baseline).* A friend
just records a booking by hand: park, site, dates, who, a note — e.g. "Paige
booked A13, Nov 23 2025." Stored locally, **no account linking, no scraping, no
credential custody at all.** It delivers most of the value (see it on the map,
coordinate the group, dedupe against your own alerts) at zero risk, and can be a
one-off per entry. Optionally seed an entry by pasting/forwarding a confirmation
email and parsing it. Each entry is **per-user private by default**, with an
option to **share it with the friend group** for trip coordination.

*A2 — Account sync (optional, deeper — credential decision PENDING).* Only if
manual logging isn't enough: link an account to pull reservations automatically.
Auth method is pluggable and you haven't decided:
- **`token`/`cookie` (recommended):** friend logs in on the real site themselves
  and provides the resulting session cookie/token; store only that. Lower blast
  radius (expires, not a password), dodges much MFA/CAPTCHA pain; re-prompt when
  it expires.
- **`password` (encrypted):** store username+password and log in automatically.
  Most seamless, highest risk (password custody, MFA/CAPTCHA breakage, lockouts).
- **`none` (email-forward / paste):** same parsing path as A1's paste option.

Build the abstraction now, pick per-platform later. Because booking stays on the
official site (B), this access only ever **reads** — even a stored password buys
read-only history, not spending power. **Start with A1; add A2 only if it earns
its keep.**

**B. Booking assist = hand-off only (your call, and the safe one).** "Assist
booking" means **load the selected site + dates into the official reservation
website** and let the friend finish there — the app never books, never touches
payment, never automates checkout. The **Book/check** action builds the deepest
deep-link the platform allows (park + campsite + date range prefilled) and opens
it in the friend's browser, where they're already logged in. All money and
booking liability stay on the official site, and the app needs **no
booking-capable credentials at all**. Deep-link depth is per-provider; worst case
it opens the campground page for the dates.

**Provider capabilities.** Extend the provider interface with optional, flag-gated
methods: `supports_account` + `fetch_reservations(link) -> list[Reservation]`, and
`deep_link(campsite, window) -> url`. Each provider declares what it supports;
providers that support neither still power the public map.

```sql
CREATE TABLE linked_accounts (
  id INTEGER PRIMARY KEY, user_id INTEGER, provider TEXT,
  auth_method TEXT,            -- 'token' | 'cookie' | 'password' | 'none'
  secret_enc BLOB,             -- encrypted at rest; key lives OUTSIDE the db (§13)
  status TEXT, last_sync TEXT, created TEXT
);
CREATE TABLE reservations (
  id INTEGER PRIMARY KEY, user_id INTEGER, provider TEXT,
  source TEXT,                 -- 'manual' (A1) | 'account' (A2)
  party TEXT, note TEXT,       -- e.g. party='Paige', note='riverside loop'
  confirmation TEXT, park TEXT, campsite TEXT,
  start_date TEXT, end_date TEXT, status TEXT,   -- 'past' | 'upcoming' | 'cancelled'
  shared_with_group INTEGER,   -- 0 = private to user (default) | 1 = visible to the friend group
  booking_url TEXT, synced_at TEXT
);
```

Security for anything you store is in §13 — encrypt with a key outside the DB,
per-user delete, never log secrets, back off on auth failures, and be transparent
with friends. This is exactly why the app is friends-only and private.

## 8k. Completeness — a campground catalog, not just search hits (never silently missing)

**The failure to avoid (real example).** Reehers Camp Horse Camp (Oregon State
Park, ReserveAmerica `parkId=412704`) had sites open — Paige just booked one — yet
it was **entirely absent** from CampSage's map. That's the worst failure mode this
app can have: a bookable place you can't even see. It happens when the map is
drawn from *search hits* or a *curated shortlist* instead of the full universe of
campgrounds.

> **CONFIRMED 2026-07-27 (Scott, firsthand).** CampSage **does not list a park
> at all if it has no available sites.** Its map is a view of *current
> availability*, not a catalog — a park that is merely full simply vanishes.
> Cross-checked against its map page source: 1044 records total, 75 in OR and
> 133 in WA, and **no Reehers**. For contrast, our federal-only catalog holds
> 355 OR + 191 WA regardless of status. The two numbers are **not** comparable
> — theirs counts what is open today, ours counts what exists — and that
> difference *is* the point of this section.

**The rule: the map is drawn from a persistent campground _catalog_; availability
is a layer on top.** Two separate things:
1. **Catalog (what exists).** Enumerate **every** campground per provider and
   persist it (`campgrounds` table): recreation.gov via RIDB facilities;
   ReserveAmerica via the full `campgroundDirectoryList` for a `contractCode`
   (which includes horse camps, boat-in, and the obscure ones); camply's
   `campgrounds` lookups; etc. Refresh **manually, or monthly to
   semi-annually** — campgrounds are close to static, so this is a maintenance
   chore, not a background job. Enumerate
   the **whole** directory, never a hand-picked list — that shortlist is exactly
   how Reehers vanished.
2. **Availability (what's open).** The scan verifies availability live and stamps
   each catalog entry's `status`.

**"No data" beats "missing."** Every catalogued campground appears on the map,
always, with an honest status:
- `available` → full size/opacity encoding (§8h).
- `full` → shown, alert-only (set a watch).
- `unknown` → catalogued but not yet checked, or the source has no availability
  read → shown with a **"no data / not checked"** cue, **not omitted.**
- `stale` → last live check failed or is old → shown as stale, **not dropped.**

This is the map-level version of the three-state rule (§8g): *absent from the
catalog* is the only reason something isn't on the map, and we work to keep the
catalog complete. A live-verification failure downgrades a pin to `unknown`/`stale`
— it never deletes it.

**Scope still applies.** The region selector (§8h) and filters bound what's drawn,
and you actively scan availability for catalogued campgrounds **in enabled
regions**. Completeness means: *within what you're looking at, nothing bookable is
silently missing.*

**Verify-live, cache-catalog.** Catalog = enumerated/scraped and cached
persistently (survives restarts and provider outages). Availability = verified
live each scan. If a provider is down you still see its full catalog as `stale`,
not an empty map.

**Search queries the catalog too.** CampSage's *search* also failed to find
Reehers (it's found fine on ReserveAmerica directly) — same root cause: it's not
in their catalog. So point search at the `campgrounds` table, and a catalogued
park is findable by name even when it's full or unchecked. Fix the catalog once,
fix both the map and search.

**Seed a static PNW catalog (dev + a reliability floor).** Don't trust live
enumeration from day one — **pre-build a static catalog of every OR/WA campground**
across providers (RIDB facilities for federal; the full ReserveAmerica OR/WA
directory; camply lookups; PerfectMind/Campspot as added) and commit it as
`data/seed/pnw_campgrounds.json`, loaded into `campgrounds` on first run. Two
payoffs:
- **A complete dev map** without depending on flaky live calls — develop and demo
  against a known-good PNW set.
- **A completeness floor:** treat the seed as ground truth for the region — diff
  each live catalog refresh against it and **keep + flag** any seeded campground a
  live enumeration dropped (exactly the Reehers case: a park that exists but a
  query missed). The catalog **never silently shrinks below the seed.**

**Fetch broad, filter locally — don't trust provider filter facets.** A provider's
own filters can silently hide bookable inventory. Concrete case: on ReserveAmerica,
Reehers returns **nothing** for a "tent" *or* "any" site-type search, but its
unfiltered park site-list lets you book a tent site (RA treats "any" differently
from "unspecified" — a bug). General rule for **every** provider: query at the
**broadest level it reliably supports** (a park's full site list for the dates) and
do site-type/attribute filtering in **our** code on the returned records. When a
provider has no clean unfiltered call, **enumerate every site type and union the
results** (the RA tactic above). A flaky provider facet then can't exclude a real
opening — our filters run on data we fully
fetched and stay three-state (§8g). Same spirit as the catalog: trust the inventory
we pulled, not the provider's facets. (Cost: fetching unfiltered is a bit heavier —
fine at personal scale, and cache-friendly.)

**How the catalog gets built (ops model).** Enumeration is **code, not a hand/chat
task** — a repo script (`catalog.py`, exposed as `manage.py catalog-refresh`) that
hits each provider's directory. Same code, two modes:
- **Dev/build → committed (one-time):** run it once, commit the result as
  `data/seed/pnw_campgrounds.json` — the reproducible seed + completeness floor.
  This is the "Claude Code runs the scraper and sticks the result in a data file"
  task. **Not** something to assemble by hand in a chat window (too large, and it
  must be reproducible/re-runnable).
- **Runtime → periodic refresh:** the app re-runs the same enumeration on a slow
  schedule — **manual, or monthly to semi-annually** — diffing against the seed
  floor (never shrink below it). Default to **manual**: a `catalog-refresh` you
  run when you feel like it, with an optional slow timer. A missed refresh costs
  nothing, because the committed seed is always the floor.

**Availability** (what's *open*) is **never** one-time — that's the continuous scan
(§8 cycle), separate from the catalog. Per-provider **site-type lists** (the enum
that drives the union tactic) are discovered once and stored as small config/data.
So: three cadences — catalog **seeded once** (committed) → **refreshed rarely
(manual / monthly–semi-annual)**
(runtime) → availability **polled continuously** (runtime).

**Manual gap-fill — right-click "find a campground here and add it."** Outdoor
tiles show campgrounds that may not be in our catalog (a scrape missed one, or it's
newly added). So give the map a **right-click action**: right-click a spot → the
app **geo-searches providers near that point** → if it finds a reservable
campground, add it to `campgrounds` and scan it; if none, say so ("no reservable
campground found here — may be dispersed/day-use"). This turns "I can see it on the
map but it's not in the app" into a one-click fix, and every add strengthens the
catalog. *Enhancement:* since OpenTopo is OSM-based, auto-surface OSM campground
POIs (`tourism=camp_site`) in view that aren't matched to the catalog, each with a
one-tap "search & add" — automating discovery instead of waiting for a right-click.

## 9. Reference material in `samples/` (what Claude Code will find)

Everything below is already in **`samples/`** in the working directory. It is
**reference-only — exclude it from the public repo** (`.gitignore samples/`, §10):
these are third-party projects with their own licenses and would bloat/entangle
your repo. For all of them you only need the **read/availability** portion — ignore
booking, captcha, login-automation, and payment code — and verify what each covers
(some may be stale). Actual folders present:

**Data engine**
- `samples/camply-main/` — **juftin/camply**, the data engine. §6/§12 are distilled
  from it; local, it lets Claude Code confirm exact per-provider constructor kwargs.

**Blueprint**
- `samples/campsage-main/` — **Zakkenroller/campsage**, the CA-only blueprint
  (Python scanner → GitHub Actions → static Leaflet map). Overall-shape reference.

**CampSage.app UI — page source (there's no public repo for it)**
- `samples/Campsage app/home page source/…Finder.html` and
  `samples/Campsage app/Map page source/CampSage — Map.html` — the actual rendered
  CampSage pages. **Primary reference for the map UI (§8h)**: marker/legend
  behavior, filter bar, popup layout. Study the markup/JS for patterns; don't copy
  wholesale (and remember we're dropping their Pro/affiliate bits, §8h). *UI
  reference only* — CampSage queries availability through its **own backend** (not
  camply), so its data logic isn't in this page source; use these files for the
  interface, not the data layer.

**ReserveAmerica** (the fiddly one, §4d/§8f — two scrapers to cross-check)
- `samples/reserveamerica-main/` and `samples/reserveamerica-master/` — the two
  independent **ReserveAmerica** scrapers (neoskx + Thrupthikk). Cross-check their
  session/viewstate + endpoint handling against each other.
- `samples/campsite-reservation-finder-main/` — **BriianPowell/campsite-reservation-finder**;
  general finder — structure + likely RA/recreation.gov request patterns.
- `samples/campingScraper-master/` — **hillenr14/campingScraper**; mine for
  parsing/paging + the availability-calendar flow.

**PerfectMind / BookMe4** (San Juan County WA, §7)
- `samples/MarkhamBooking-main/` — **quantformity/MarkhamBooking**, a BookMe4
  automation showing the endpoint/session flow. *(Only this one PerfectMind
  reference is present; the earlier SeanXLChen/nvrc-perfectmind-booking isn't in
  the folder — one solid example is enough, add nvrc later if you want a second.)*

For ReserveAmerica, use these to learn its session/viewstate handling and the
`unifSearch.do` / `campsiteCalendar.do` / per-site detail endpoints (plus the
**union-all-site-types** tactic, §4d), then keep your provider **read-only and
gentle** (slow interval, realistic headers, hard caching) — RA guards its traffic.

## 9b. What the CampSage page source confirms (from `samples/Campsage app/…Map.html`)

Analyzed the saved map page — legitimately-reusable findings:

**Backend API surface (all under `/camp/`):**
- `/camp/full-data` → tracked campgrounds **with** availability (the live pins).
- `/camp/ref-data` → **untracked** campgrounds (county/district/private) as small
  gray pins, **no availability** — the "📍 Other campgrounds" toggle. *CampSage
  already has a two-layer idea (tracked + untracked) — but that layer is **off by
  default** and still missed Reehers. Our §8k goes further: one **complete**
  catalog, always on, every pin clickable-for-why.*
- `/camp/catalog-lite` → lightweight catalog for search.
- `/camp/<slug>/weather` → per-campground NWS weather (async, non-blocking).
- `/camp/go?to=<bookingUrl>` → booking **redirect handoff** (wrapped for tracking;
  ours is a direct deep-link, §8j-B).
- `/camp/pro/*`, `/camp/premium/interest`, `/camp/event`, `/camp/push/register`,
  `/camp/watchlist/send`, `/camp/notify` — the Pro/analytics/alerts plumbing we
  deliberately drop.

**Pin data shape (a good model for our API):** `{lat, lng, name, slug, st, k(kind),
full(bool), n2/n3(has 2-/3-night), jo(just-opened), dates:[{s:startISO, n:nights}],
url, rating, reviews, img, cv(cell 0–5), parent, region}`.

**Rendering (confirms our stack + where we improve):**
- **Leaflet + markercluster + `preferCanvas:true`** — adopt this.
- **Plain OSM tiles** (`tile.openstreetmap.org`) — CampSage has **no trail
  detail**; our OpenTopo/outdoor tiles (§8h) are a real improvement.
- Marker **color by nights** (`full→gray, beach→orange, n3→green, n2→blue`) and
  **fixed radius** (7, or 9 if just-opened). We deliberately differ: **size =
  count, opacity = date × AQI, color reserved** (§8h) — denser encoding.
- Date filtering uses **window-overlap ≥ min-nights** over each pin's `dates[]` —
  exactly the logic our slider needs (§8h).

**Cell coverage — the useful correction:** their signal isn't FCC, it's
**recreation.gov camper reports** per carrier (`≥3/5 = good`). Folded into §8e as
the easy primary source.

## 10. Hosting: Docker + Tailscale

**Repo layout** (built at the top level of `CampgroundFinder/`; `samples/` and
`.env` are gitignored so the **public repo is just the app + this plan**)
```
CampgroundFinder/                    # = the public repo root
  campgroundfinder-build-plan.md     # this doc (fine to commit)
  README.md
  docker-compose.yml
  Dockerfile
  requirements.txt           # fastapi uvicorn[standard] apscheduler apprise pyyaml httpx camply
  .gitignore                 # MUST include: samples/  .env  data/*.db
  .env.example               # notification secrets (Apprise URLs / tokens) — real .env NEVER committed
  config.example.yaml        # home base, sources, scan interval, notify defaults, access
  app/
    web.py                   # FastAPI: API + static + starts APScheduler
    config.py  db.py  store.py  catalog.py  scanner.py  notifier.py  util.py
    providers/ base.py camply_provider.py reserveamerica.py perfectmind.py mock.py __init__.py
    static/ index.html app.js styles.css
  scripts/ manage.py         # CLI: catalog-refresh, scan-once, list-providers, add-watch
  tests/ test_core.py        # mock provider → store → watch match (no external deps)
  data/                      # sqlite lives here (volume)
    seed/pnw_campgrounds.json  # static OR/WA catalog seed + completeness floor (§8k)
  samples/                   # <-- REFERENCE ONLY, gitignored (camply, CampSage page source, RA/PerfectMind scrapers — §9)
```

**docker-compose.yml** (web + optional Tailscale sidecar)
```yaml
services:
  campgroundfinder:
    build: .
    container_name: campgroundfinder
    restart: unless-stopped
    volumes:
      - ./data:/data
      - ./config.yaml:/app/config.yaml:ro
    env_file: .env
    ports:
      - "127.0.0.1:8080:8080"        # localhost only; Tailscale exposes it privately

  # Option B — publish over Tailscale from a sidecar (uncomment to use):
  # tailscale:
  #   image: tailscale/tailscale:latest
  #   hostname: campgroundfinder
  #   environment:
  #     - TS_AUTHKEY=${TS_AUTHKEY}
  #     - TS_SERVE_CONFIG=/config/serve.json   # serves http://campgroundfinder:8080 as HTTPS
  #     - TS_STATE_DIR=/var/lib/tailscale
  #   volumes:
  #     - ./tailscale:/var/lib/tailscale
  #   cap_add: [net_admin, sys_module]
  #   restart: unless-stopped
```

**Tailscale — two ways:**
- *Simplest (host-level):* on the **host (the always-on Linux Mac mini)**, run the
  stack, then `tailscale serve --bg 8080`. You get
  `https://<mini>.<tailnet>.ts.net` reachable from any device on your Tailnet,
  with automatic HTTPS. Nothing public. (Install Tailscale on the mini, not the
  M1 — the mini is what serves the app.)
- *Sidecar:* uncomment the `tailscale` service, supply a `TS_AUTHKEY`, and a
  `serve.json` that maps the container. Good if the host isn't itself on the
  Tailnet.
- *Adding a friend (invite-only):* in the Tailscale admin console, **share** the
  CampgroundFinder machine with that friend's Tailscale account (Machines → the device
  → Share, which generates an invite link). They install Tailscale (free), accept
  the share, and open the same `https://<machine>.<tailnet>.ts.net` URL. Their
  Tailscale identity becomes their login automatically (§13) — no account setup
  in the app. Revoke a friend by un-sharing the device.
- *Wider access (Funnel + app accounts):* to include people **not** on your
  Tailnet, run `tailscale funnel` (public HTTPS) — but **only with app-level auth +
  manual approval on** (§13). Note: a **GitHub Pages** "public page" can host a
  static landing/README only; the live app (backend + scanner + DB) **can't** run
  there — it runs from your container, exposed via Serve (private) or Funnel
  (public).

**Dockerfile** (sketch)
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
ENV CAMPGROUNDFINDER_DB=/data/campgroundfinder.db CONFIG_PATH=/app/config.yaml
EXPOSE 8080
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080"]
```

**Two-machine topology (important — dev ≠ host).** Development and hosting are
**separate machines**:
- **Dev — Mac Studio (M1 / ARM):** where you edit, run Claude Code, and use git.
  **No Docker needed here** — don't fight Docker-on-M1. Claude Code runs the
  stdlib-only tests; optional fast preview via a local venv + `uvicorn app.web:app
  --reload`. *(Update 2026-07-27: camply 0.34.2 is now installed in `.venv/` on
  the M1 and a live recreation.gov search works from there, so this is no
  longer a manual step.)*
- **Host — Intel Mac mini running Linux (x86), always-on, on your LAN:** the actual
  Docker host. It clones the **public** repo (no auth needed for a public pull),
  builds the image **natively for x86** (no ARM/x86 mismatch, real network for PyPI
  + Docker Hub), and runs the app + scanner + Tailscale **24/7** — which is exactly
  where always-on scanning and alerts belong (a laptop sleeps; the mini doesn't).

**Deploy loop (GitHub is the pipe between the two machines):**
1. *One-time on the mini:* `git clone https://github.com/dscottfrey/CampgroundFinder.git`,
   create `config.yaml` + `.env` **on the mini** (secrets live only there, never in
   the repo), then `docker compose up -d --build`.
2. *Each update:* edit on the M1 → commit + push to GitHub → on the mini
   `git pull && docker compose up -d --build`. Wrap that in a one-line `deploy.sh`
   on the mini (or run it over SSH from the M1). The SQLite volume + config are
   mounted, so **data survives rebuilds**.

Build happens **on the mini**, so the image always matches that box's architecture
— never build on the M1 and try to run it on the mini. Because the repo is
**public**, the mini pulls with zero credential setup.

## 11. Notifications (Apprise = "everything")

Apprise gives one syntax for ntfy, Telegram, Slack, email, Pushover, Discord,
Twilio SMS, webhooks, etc. Watches store a list of Apprise target URLs; the
notifier fans out. For a self-hosted Tailnet stack, **ntfy** is the easiest phone
push. Examples for `notify_targets` / `.env`:
```
ntfy://ntfy.sh/my-secret-campgroundfinder-topic
tgram://<bot_token>/<chat_id>
mailto://user:pass@smtp.example.com?to=you@example.com
slack://<tokenA>/<tokenB>/<tokenC>
```

## 12. Verified camply facts (appendix — trust these over guesses)

- Import surface: `from camply.search import CAMPSITE_SEARCH_PROVIDER` (dict
  keyed by **provider** name, e.g. `"RecreationDotGov"` → class — **NOT**
  `"SearchRecreationDotGov"`, which this appendix originally claimed.
  VERIFIED 2026-07-27 against installed camply 0.34.2);
  `from camply.containers import SearchWindow, AvailableCampsite`.
- `SearchWindow(start_date: date, end_date: date)`; past start dates are coerced
  to today.
- `BaseCampingSearch.__init__(search_window, weekends_only=False, nights=1,
  offline_search=False, offline_search_path=None, days_of_the_week=None,
  **kwargs)`. Provider subclasses accept `recreation_area`, `campgrounds`,
  `campsites` via `**kwargs`.
- One-shot search: `get_matching_campsites(log=True, verbose=False,
  continuous=False, polling_interval=None, notification_provider="silent",
  notify_first_try=False, search_forever=False, search_once=False) ->
  list[AvailableCampsite]`. Use `continuous=False` to get a plain list back.
- `AvailableCampsite` fields: `campsite_id`, `booking_date` (datetime),
  `booking_end_date`, `booking_nights` (int), `campsite_site_name`,
  `campsite_loop_name` (opt), `campsite_type` (opt), `campsite_occupancy`,
  `campsite_use_type` (opt), `availability_status`, `recreation_area`,
  `recreation_area_id`, `facility_name`, `facility_id`, `booking_url`,
  `location` (`.latitude`, `.longitude`), `permitted_equipment` (list),
  `campsite_attributes` (list).
- CLI (equivalent path, if you prefer subprocess over the library):
  `camply campsites --provider <P> --rec-area <id> --start-date YYYY-MM-DD
  --end-date YYYY-MM-DD --nights N [--weekends] [--search-forever
  --polling-interval M] [--notifications <backend>]`. Lookups:
  `camply recreation-areas --search … --state … --provider …`,
  `camply campgrounds --rec-area … --provider …`.
- Notification backends camply ships: `silent, Email, Slack, Twilio, Pushover,
  Pushbullet, Ntfy, Apprise, Telegram, Webhook` (configured via env vars). You
  can lean on these instead of Apprise if you'd rather delegate alerts entirely
  to camply's `--search-forever`.

## 13. Operational guidance & gotchas (read before building)

**Be a polite client — the difference between "works" and "IP-banned."**
recreation.gov blocks datacenter IPs and rate-limits aggressive callers (the
blueprint repo hit this on scheduled cloud runs). Running from home keeps you
clear — stay that way: don't scan more often than needed (`scan_interval_minutes:
30`, never below ~10), set a descriptive `User-Agent`, stagger providers instead
of firing all at once, and back off on HTTP 429/403 (exponential; skip that
provider for the cycle). Bound search windows — availability is fetched
per-month, so a 6-month window across many rec areas is a lot of calls.

**camply upkeep.** Pin the version in `requirements.txt` and re-check the adapter
when you bump it (the API in §6/§12 is verified against current source but can
shift). Verify per-provider constructor kwargs against the local clone (§6). If
the library fights you, the CLI path (§12) is the more stable contract. Optional:
a free **RIDB API key** (ridb.recreation.gov) buys richer facility metadata
(amenities, images, coordinates) — not required for core function.

**Data realities.** Some sites have no lat/lon (PerfectMind especially) — show
them in the list as "location unknown" and exclude them from distance filtering
rather than dropping them silently. Treat the DB as a cache with `last_seen` and
prune aggressively so the map reflects *now*, not an hour ago.

**Access & multi-user (public repo, gated app, no anonymous view).** The **code
repo is public** — it holds no secrets (all credentials are runtime-only in
`.env`/DB, **never committed**; commit only `.env.example`). How wide the *running*
app is exposed is your call; two auth modes cover the range:
- **Private — Tailscale identity (no passwords).** Run behind `tailscale serve`
  and **share the CampgroundFinder machine** with each friend's Tailscale account.
  `tailscale serve` injects the caller's identity as headers (`Tailscale-User-Login`,
  `Tailscale-User-Name` — confirm names in current Tailscale docs); the app reads
  those as the user, no passwords. Must-dos: bind only to loopback/tailscale, and
  **strip client-supplied `Tailscale-User-*` headers** (trust only the tailscaled
  proxy).
- **Wider but gated — app accounts you approve.** To reach people **not** on your
  Tailnet, expose via **Tailscale Funnel** (or a small public host) and add
  **app-level accounts with manual approval**: someone signs up → lands in a
  **pending queue** → **you log in as admin and approve** them from an admin panel
  (an email ping on new requests is a nice extra). **No approved account → they
  see nothing** — fine, nothing's for sale, so no anonymous teaser is needed.
  Passwords hashed (argon2/bcrypt) or magic-link; standard session auth, since
  Funnel users aren't on the Tailnet.

On first visit an authenticated user (either mode) **auto-provisions/attaches** to
a `users` row (`status='pending'` until you approve for the app-account mode;
Tailscale-shared friends can auto-approve). You're `admin`; watches + notify
targets stay **per-user**; the map is shared. **Only turn on Funnel once you've
confirmed the app is fully gated** (no anonymous route leaks data) — the
public-repo/public-serve plan is safe *precisely because* there are no credentials
in the repo and no anonymous access to the app. Keep `.env` out of git; mount
SQLite on a volume so data survives `docker compose up --build`.

**Linked-account secrets (only if you build the optional §8j-A reservation
view).** Encrypt each stored token/password at rest with a key held **outside**
the DB (env/secret file or OS keyring), per user; never log secrets; let each
friend view and delete their own links; sync on a modest schedule and back off on
auth failures so you never trigger a lockout; be transparent with friends about
what's stored. The booking **hand-off** (§8j-B) needs none of this — it stores no
credentials, so if you skip the reservation view you carry **zero** credential
risk.

**Correctness.** Set the container timezone (`TZ=America/Los_Angeles`) so
weekends-only, "tonight", and date math match your local days. Write test
fixtures — capture one real JSON response per provider and replay it in `tests/`
so you develop normalization/filters offline without hammering live APIs.

## 14. Suggested build order for Claude Code

1. Scaffold repo + `providers/base.py` + `mock.py`; write `store.py` on stdlib
   `sqlite3`; get `tests/test_core.py` green (mock scan → store → watch match).
   *No external deps needed for this step — it's fully unit-testable.*
2. Add `camply_provider.py` (§6), `pip install camply`, and smoke-test against a
   real rec-area you care about (Mt Hood / Deschutes NF).
3. Build `catalog.py` (§8k): first **seed the static PNW catalog**
   (`data/seed/pnw_campgrounds.json` — every OR/WA campground across providers),
   then live-enumerate and **diff against the seed** (keep + flag anything the
   live query dropped; never shrink below seed). Add `scanner.py` (verify
   availability live over the catalog, stamp status) + `notifier.py` (Apprise) +
   `config.py`; run `catalog-refresh` then `scan-once` from `scripts/manage.py`.
   Point **search at the catalog**. Acceptance test: Reehers Camp Horse Camp is
   both **findable by search** and **shown on the map even when full**.
4. Add the **enrichers & three-state filters**: AQI (§8d, Open-Meteo + per-cell
   cache), wildfire proximity (§8e, FIRMS/WFIGS fetched once per cycle), water
   level (§8e, advisory), weather + cell coverage (§8e), and normalized attribute
   filters (§8f). Implement the
   **pass/fail/unknown** engine (§8g) once and reuse it for all of them — store
   values, compute state at query time, never let unknown read as fail. Default
   `green_only=true`.
5. FastAPI `web.py` + static Leaflet UI (map + region selector + filters +
   green-only toggle + watches CRUD).
6. `Dockerfile` + `docker-compose.yml`; bring it up; `tailscale serve --bg 8080`.
   Wire **multi-user** (§13): read the `Tailscale-User-*` identity headers →
   look up / auto-provision a `users` row, scope watches + notify targets to that
   user, seed yourself as admin, and strip client-supplied identity headers.
   Share the machine with a friend to test a second identity.
7. Implement `perfectmind.py` against San Juan County WA using the §9 refs.
8. Add remaining sources — **ReserveAmerica / Oregon State Parks first** (custom;
   richest attributes, makes tent-pad + vehicle-length hard filters, §4d/§8f),
   then BC (GoingToCamp), Parks Canada + Campspot (custom, §4d), and any other
   camply providers you want in `config.yaml`.
9. *(Optional)* Booking **hand-off** deep-links are part of the UI (step 5) and
   need no credentials — do them anytime. For trip history, build the
   zero-credential **manual trip log** (§8j-A1) first — it's a small CRUD form and
   covers most of the value. Only add **account sync** (§8j-A2, the
   `AccountLink` abstraction + a chosen credential method) if manual logging isn't
   enough. Don't take on credential custody unless the feature earns it.

---

*Built from research on the CampSage web app, the Zakkenroller/campsage repo
(the CA-only blueprint), and camply's source. The camply API in §6 and §12 is
verified; the PerfectMind provider is a designed stub to finish locally against
the live widget.*
