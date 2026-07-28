# ReserveAmerica scraper — build handoff

Everything needed to build and run the ReserveAmerica (Aspira) provider.
Written 2026-07-27. **Nothing in here has been tested against the live site.**
Treat every endpoint, parameter, and ID below as unverified until proven.

## Why this provider is mandatory, not optional

Reehers Camp Horse Camp is bookable and **invisible to every ReserveAmerica
search**. Observed firsthand on the live site:

| Search | Reehers appears? |
|---|---|
| tent sites | no |
| any sites | no |
| horse sites (it *has* horse sites) | no |
| pick Reehers from the master park list | **yes — a tent site can be reserved** |

So the build plan's original fix — "enumerate every site type and union the
results" — **does not work.** Unioning zero results is still zero.

**The only method that works: never search. Walk the full park directory, then
ask each park directly.** Reehers is the proof that §8k's catalog model is
mandatory here.

## Scope and priority

Scott wants **western states only**; the rest aren't needed.

1. **OR** — first. Contains Reehers, the acceptance case.
2. **WA** — second.
3. Then the other western contract codes, if wanted.

Skip the eastern states entirely unless asked.

## Rate limiting — the hard constraint

This runs from **Scott's home internet**, which serves his household. The plan
(§4d, §13) says ReserveAmerica guards its traffic harder than any other source.
**Getting this IP banned is worse than never shipping the provider.**

- One request at a time. Never parallel.
- **5–10 seconds between requests.** Slower than the 2s used for RIDB.
- Full stop on the first 429 or 403. Do not retry into a block.
- Cache every response to disk during development so a re-run costs nothing.
- **First full run happens overnight, with Scott's approval**, not unattended
  on a whim.
- Scott can put traffic on a **VPN in another state** — ask before a big run,
  and note that a VPN exit may itself be blocked (datacenter IPs are what
  recreation.gov blocks; RA may be similar).

Rough budget: Oregon has a few hundred parks. At 8s apart that's roughly an
hour for the directory plus one availability call each. That is the intended
pace — it is not a bug.

## What to build

`app/providers/reserveamerica.py`, subclassing `Provider` from
`app/providers/base.py`. Two methods matter:

```python
def list_campgrounds(self, state=None, rec_area_ids=None) -> list[Campground]
def search(self, req: SearchRequest) -> list[Campsite]
```

`list_campgrounds` walks the directory. `search` queries one park's
availability. Both already have callers — `catalog.refresh_catalog()` and
`scanner.scan_source()` — so nothing else needs to change.

Register it in `app/providers/__init__.py:build_provider()`, which currently
raises `NotImplementedError` for `ReserveAmerica:*`.

## Endpoints — ALL UNVERIFIED

From the build plan and the two scraper repos in `samples/`. Confirm each in
browser devtools (Network → XHR) before writing code against it.

| Purpose | Endpoint (unverified) |
|---|---|
| Park directory for a contract | `campgroundDirectoryList` |
| Park search | `unifSearch.do` — **do not use, see above** |
| Park availability calendar | `campsiteCalendar.do` |
| Per-site detail | site detail call, name unconfirmed |

- Contract code for Oregon: `contractCode=OR`, host
  `oregonstateparks.reserveamerica.com`.
- Reehers parkId: **412704 — UNVERIFIED**, taken from the build plan, which has
  been wrong about every other ID checked so far. Verify before relying on it.
- The site is ASP.NET: expect a **session cookie plus viewstate** handshake.
  Establish the session once and reuse it across the whole scrape; refresh on
  expiry rather than re-handshaking per request (§6c).
- Use an **honest, descriptive User-Agent**. Do not rotate or fake it (§6c).

## Cross-check against samples/

Two independent scrapers, neither tested by us:

- `samples/reserveamerica-main/` (neoskx)
- `samples/reserveamerica-master/` (Thrupthikk)
- `samples/campingScraper-master/` — availability-calendar flow and paging
- `samples/campsite-reservation-finder-main/` — general request patterns

Where the two RA scrapers disagree, trust neither; check the live site.
Read only the availability parts — ignore booking, login, and payment code.

## Definition of done

1. `list_campgrounds(state="OR")` returns the **full** Oregon park directory,
   including horse camps and boat-in sites.
2. **Reehers appears in that list**, and its tent sites appear in `search()`
   for a date range where the live site allows booking.
3. Each campground carries lat/lon where available; `None` where not — never a
   guessed coordinate.
4. Re-running is idempotent and never shrinks the catalog (§8k).
5. Add the result to `data/seed/pnw_campgrounds.json` via
   `manage.py catalog-refresh --write-seed`.

## State of the rest of the catalog

Already done — 546 campgrounds in the seed, live from RIDB on 2026-07-27:

| Source | Status |
|---|---|
| recreation.gov (federal) | **done** — 355 OR, 191 WA, coords, reservable flags |
| ReserveAmerica | this document |
| GoingToCamp (WA parks, BC parks) | **blocked** — camply cannot enumerate without a rec-area ID; `washington.goingtocamp.com` also does not answer from the dev sandbox though it loads fine in a browser |
| UseDirect (OregonMetro, ReserveCalifornia) | **blocked** — same: needs a search string or rec-area ID, cannot enumerate blind |
| PerfectMind (San Juan County) | not built (step 7) |
| Parks Canada, Campspot | not built (step 8) |

GoingToCamp and UseDirect need a one-time list of rec-area IDs in
`config.yaml` before they can contribute. See below.

---

# GoingToCamp — build handoff

Covers **Washington State Parks** and **BC Parks**, both of which Scott wants.
camply already speaks this platform, so this should be config plus a small fix
— not a new scraper.

## The rec-area IDs (verified in camply 0.34.2)

Read from `camply/providers/going_to_camp/rec_areas.py` — 17 areas total.
The ones that matter here:

| Host | Rec area | ID |
|---|---|---|
| `washington.goingtocamp.com` | Washington State Parks | **3** |
| `tacomapower.goingtocamp.com` | Tacoma Power Parks (WA) | **6** |
| `camping.bcparks.ca` | BC Parks | **12** |

This settles the §4d open question: **BC Parks is already in camply.** It is a
config entry, not a custom provider.

## Two blockers, both unresolved

**1. camply cannot enumerate without a rec-area ID.** Calling
`list_campgrounds()` with only a state raises "This provider requires
--rec-area to be specified". Unlike RIDB there is no directory call. Hence the
table above — those IDs have to be supplied from config.

**2. Supplying the ID still fails.** Tried live on 2026-07-27:

```
GoingToCamp().find_campgrounds(rec_area_id=[3])
  -> KeyError: -2147483647
```

**That number is not a bug sentinel — it is a real park.** Confirmed from
CampSage's map page source: `-2147483647` is the `resourceLocationId` for
**Alta Lake State Park**, the first WA state park alphabetically. So camply
reached the platform, got a real park id back, and then failed looking it up in
some internal map — most likely its rec-area/map cache was never populated for
`rec_area_id=3`.

*(An earlier draft of this document guessed `-2147483647` was `INT_MIN+1` used
as a "nothing selected" sentinel. That guess was wrong.)*

Also unresolved: `washington.goingtocamp.com` does not answer from the dev
sandbox (connection times out) even though it loads normally in a browser, and
`camping.bcparks.ca` returns 403 to a bare request.

## The endpoint shape — from CampSage's page source

CampSage does **not** use camply; it calls GoingToCamp directly. Its map page
embeds 69 Washington State Parks, each with a booking deep-link:

```
https://washington.goingtocamp.com/create-booking/results
    ?resourceLocationId=-2147483647     # the park
    &mapId=-2147483396                  # the site map within the park
    &bookingCategoryId=0                # 0 = camping
    &startDate=YYYY-MM-DD
    &isReserving=true
    &partySize=1
```

So each park is identified by a **(resourceLocationId, mapId)** pair, both large
negative integers. Examples verified present in the page source:

| Park | resourceLocationId | mapId |
|---|---|---|
| Alta Lake State Park | -2147483647 | -2147483396 |
| Battle Ground Lake State Park | -2147483646 | -2147483395 |
| Bay View State Park | -2147483645 | -2147483394 |
| Belfair State Park | -2147483643 | -2147483319 |
| Birch Bay State Park | -2147483641 | -2147483393 |

All 69 pairs, with names and coordinates, are extractable from
`samples/Campsage app/Map page source/CampSage — Map.html`.

**Caveat on using them:** these come from a third party's page, are a snapshot,
and §9 of the main plan says study the patterns, don't copy wholesale. Use them
to learn the ID scheme and to cross-check; confirm against the live site before
committing them to our catalog.

## Suggested order

1. Reproduce the `KeyError` with camply's own CLI:
   `camply campgrounds --provider GoingToCamp --rec-area 3`. If the CLI works,
   the bug is in how we call it. If it fails too, it is camply's.
2. Check camply's issue tracker for that KeyError before debugging it yourself.
3. **If camply stays broken, go direct** — the URL shape above is the whole
   contract, and it is the path CampSage actually uses. Find the JSON XHR the
   `create-booking/results` page fires (§6c) and replay that. This may well be
   less work than fixing camply.
4. Once one rec area enumerates, add all three to `config.yaml` as sources
   (states `WA`, `WA`, `BC`) and let `catalog-refresh` pick them up — the
   existing `CamplyProvider.list_campgrounds()` path already handles the
   non-RIDB case.
5. Pace it the same as everything else: one request at a time, seconds apart.

## Definition of done

- Washington State Parks campgrounds appear in the catalog with a `WA` region
  tag, BC Parks with `BC`.
- The region selector shows BC without the "no source configured" hint.
- Coordinates where the platform provides them, `None` where it does not —
  never guessed.

---

# UseDirect (OregonMetro, ReserveCalifornia, and the state-park systems)

Same shape of problem: `find_campgrounds()` refuses without a search string,
campground ID, or rec-area ID — "You must provide a search string, campground
ID, or recreation area ID to search on UseDirect". There is no blind
enumeration.

Unlike GoingToCamp, camply has **no built-in rec-area table** for these, so the
IDs have to be discovered once via
`camply recreation-areas --provider OregonMetro --state OR` and then committed
to config. Lower priority than ReserveAmerica — OregonMetro is a handful of
regional parks, while ReserveAmerica is every Oregon state park including
Reehers.
