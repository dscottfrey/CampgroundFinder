# CampgroundFinder

A personal, self-hosted campsite availability tracker for the Pacific Northwest.
Scans reservation providers on a polite schedule, keeps a **complete campground
catalog** (not just search hits), and alerts you when a spot you want opens up.

Design and rationale live in [`campgroundfinder-build-plan.md`](campgroundfinder-build-plan.md).
Section references below (§5, §8k, …) point into it.

## Status — last updated 2026-07-28

| Step | Scope | State |
|---|---|---|
| 1 | Scaffold, providers, `store.py`, core tests | **done** |
| 2 | camply adapter | **done, verified live** against recreation.gov |
| 3 | `catalog.py`, `scanner.py`, `notifier.py`, `config.py`, `manage.py` | **done**; 803 real campgrounds seeded |
| 4 | Enrichers (AQI, wildfire, water, weather) + three-state filters | not started |
| 5 | FastAPI + Leaflet UI | **partial** — a stdlib server and list view exist; no map |
| 6 | Docker + Tailscale + multi-user | not started |
| 7 | PerfectMind provider | not started |
| 8 | ReserveAmerica | **Oregon done** |
| 8 | GoingToCamp | **done** — WA state parks + BC Parks |
| — | Pacing: shared rate limiter, round-robin, scanner status | **done** (`docs/scanning-design.md` steps 1–2) |

**171 tests, standard library only, no network required.**

### What actually works

- **803 real campgrounds**, all enumerated live, none hand-listed:
  545 recreation.gov (RIDB), 65 Oregon state parks (ReserveAmerica),
  79 Washington state parks and 114 BC Parks (GoingToCamp). **774 carry
  coordinates**; the remaining 29 show as "location unknown" rather than being
  dropped or guessed. Reehers Camp Horse Camp is in there at 45.7067, -123.3381.
- **recreation.gov availability**, through camply. Verified: Trillium
  (campground 232831) returns real openings with booking links.
- **ReserveAmerica for Oregon** — all 65 state parks in the committed seed
  with coordinates, plus per-park availability. Reehers Camp Horse Camp,
  parkId 412704: **17 sites** — 10 horse (loop A, including the host pitch)
  and 7 tent (loop B). The build plan's "20 horse and 14 tent" was never
  true; verified against the live page 2026-07-28. A source with an empty
  `campground_ids` now scans every park in the catalog, one per request —
  about 6m30s for a full Oregon pass.
- **A web page** — `manage.py demo`, then http://127.0.0.1:8080. List view
  only; the map is not built.
- **Pacing that can't be bypassed.** Every upstream request in the process goes
  through one rate limiter (`app/pacing.py`): one request at a time, 6s apart
  for ReserveAmerica and 2s for recreation.gov, spaced from when the last
  response landed. A 403 or 429 latches that host off for an hour instead of
  retrying. Sources are scanned **round-robin**, one campground at a time, so
  consecutive hits on any single host are as far apart as possible.
- **The scanner says what it's doing.** A `scan_status` row carries
  "Checking 8 campgrounds — 3 done" plus the reason for any wait, and rides
  along in `/api/state`. Parks a block prevented us from checking are marked
  **stale**, never "full" — "we didn't look" must not read as "nothing there".

### Where we left off — read this first

**GoingToCamp is unblocked** (2026-07-28) — Washington State Parks and BC
Parks are in. Two things were in the way:

*The WAF.* Both hosts sit behind an Azure WAF that 403s anything not shaped
like a browser — the honest User-Agent was the only thing being blocked. See
`docs/scraping-policy.md`, which supersedes build plan §6c.

*camply's bug.* `KeyError: -2147483647` is Alta Lake State Park, and the cause
is now known exactly: camply builds a lookup from `/api/maps`, which returns
six org-level maps whose `resourceLocationId` is `null`, so the dict is keyed
entirely by `None` and every park misses. `going_to_camp_provider.py:427` is a
bare subscript whose result is discarded — it exists only to raise. We don't
use that endpoint; `rootMapId` is already on every directory record. So
`app/providers/goingtocamp.py` talks to the API directly and skips camply.

**Next build step, per `docs/scanning-design.md`:** step 3 — the background
sweep with adaptive cadence, then step 4 (on-demand refresh, all four guards)
and step 5 (zoom-based queue priority). Steps 1 and 2 — the shared rate limiter
and the scanner status row — are done, so the pacing groundwork wider scanning
needed is in place.

One caveat carried forward: camply owns its own HTTP, so its several internal
requests per search can't be spaced individually. The adapter holds the shared
request slot around the whole call, but camply's internal pacing is unverified —
worth checking before recreation.gov carries on-demand traffic.

### Two honesty bugs found by asking "what's actually in the catalog?"

Both were the map asserting something it did not know — the Reehers failure
inverted. Scott found them by asking whether the 803 figure included parks that
can't be scanned.

1. **206 first-come campgrounds were being marked `full`.** They have no
   reservation feed, so a scan finding nothing there is exactly as informative
   as not looking. "Full" would send someone driving past a campground with
   space. They now read `unknown`, with the reason spelled out.
2. **A rec-area-scoped source stamped the whole state.** Scanning Mt Hood
   marked every Oregon recreation.gov campground `full`, coastal ones included,
   though they were never queried. A scan now stamps only what it can speak
   for. Where the covered set is genuinely unknowable — `Campground` doesn't
   record which rec area it belongs to — it stamps only what came back and
   leaves the rest alone. Silence beats a confident wrong answer.

### Three source-data rules, learned the hard way

All ground-truthed by Scott against campgrounds he has actually stayed at.
Each one, applied naively, would have hidden a bookable site — the Reehers
failure with a new cause each time.

1. **"TENT SITE" is not an equipment restriction.** Beverly Beach C27 is a
   TENT SITE he has camped in a van. Only an explicit "RV prohibited" in the
   site description says otherwise, and we don't read descriptions — so
   "does my van fit?" is *unknown*, and unknown is shown, not filtered away.
2. **The driveway length is a floor, and how much you trust it depends on how
   often it repeats.** No real campground has most of its sites the exact same
   length — a forested loop bends around trees. So a value repeated across a
   park is a form default and means nothing; a rare value was entered
   deliberately and should be believed. `app/equipment.py` detects this and
   answers in three buckets, never merging "fits" with "no idea". At Beverly
   Beach a 40 ft rig gets **0 confirmed fits, 21 unknown, 4 no** — not 21 fake
   candidates.

   Original note: **the driveway length is a floor, not a measurement.** A park manager told
   Scott they had no staffing to measure when going onto ReserveAmerica, so
   most sites carry a default — 21 of 24 on one loop read exactly "20 Back-In".
   A01 lists 20 ft and is really 53; A15, genuinely short, was entered
   accurately at 15. So listed ≤ actual: a longer rig may still fit, and
   `fits_equipment()` answers *unknown* rather than *no*. Blank is the one
   confident no — that's a walk-to site.
3. **The site-type icon is simply wrong.** All 21 Brooke Creek WALK TO sites
   carry an `rv` icon and cannot take an RV.

### First-come: two separate claims

"This campground takes no reservations" and "this bookable campground *also*
has first-come sites" are different facts, and we usually know only the first.
They're modelled separately, three-state per §8g:

| `reservation_type` | `first_come_sites` | shown as |
|---|---|---|
| `first_come` | — | First-come, first-served — no reservations |
| `reservable` | `True` | Reservable, and some sites are first-come |
| `reservable` | `False` | Reservable — every site is bookable |
| `reservable` | `None` *(default)* | Reservable |

The last row is the point: when we don't know, the label **says nothing** about
first-come sites rather than implying there are none.

Nothing populates `first_come_sites` yet. `docs/first-come-research.md` has the
findings: RIDB *can* tell us how many sites at a reservable campground aren't
bookable online (BROKEN ARROW: 145 of 279), but the 206 first-come campgrounds
have **no site inventory in RIDB at all** — so the number is unavailable
exactly where it would be most useful. And `CampsiteReservable=False` is not a
synonym for first-come: some of those sites are `MANAGEMENT`, i.e. camp-host
pitches. The honest phrasing is "not bookable online", not "first-come and
maybe free".

### The bug the live run found (2026-07-28)

Worth reading before trusting any pager in this codebase.

ReserveAmerica sends `Transfer-Encoding: chunked` and **ends about half its
responses without the terminating chunk**. A truncated page is HTTP 200, looks
like HTML, and parses to zero park rows — which was indistinguishable from the
end of the directory, because the loop's terminator was "this page had no new
parks". Enumeration stopped at **25 of 65 parks**, alphabetically A through C.
Reehers starts with R. The acceptance case of this entire project would have
silently vanished again, from a brand-new cause.

Two fixes, both in `app/providers/reserveamerica.py`:

1. Every page is checked for completeness — the listing table's close, which is
   present in complete pages and absent from every truncated one we've seen.
   Checked on *all* pages, not just empty ones: a page cut off mid-listing
   yields some rows, advances the offset, and leaves a hole in the middle.
   A truncated page is retried once (it is not a block signal), then
   `IncompleteDirectory` is raised rather than a short list returned.
2. The transport prefers `requests` over the standard library. Measured on the
   same page: `requests` 176 KB complete, `urllib` 74 KB truncated every time.
   `http.client`'s chunked decoder is the stricter one, and this server needs
   the tolerant one.

### Two cautions worth remembering

1. **The build plan's data is unreliable.** Nearly every concrete value checked
   so far was wrong — camply's registry keys, the provider count, campground
   232876, rec area 1106, and Parks Canada's absence from camply. Corrected
   entries are marked `VERIFIED <date>`; anything unmarked is still a guess.
   The one plan value that turned out correct was Reehers' parkId.
2. **Estimates from Claude are also worth questioning.** The cost of a
   ReserveAmerica sweep was reported as 4 hours; it is about 7 minutes. Scott
   caught it by reasoning that CampSage could not possibly be doing a 4-hour
   scan per state. The fix was a query parameter that had been missed.

### Reference documents

| File | What's in it |
|---|---|
| `docs/terminology.md` | **first-come vs hike-in — two axes, never say "walk-up"** |
| `docs/scraping-policy.md` | **How we behave toward platforms — supersedes §6c** |
| `docs/bc-coordinates.md` | Why BC parks had no coordinates, and how it was fixed |
| `docs/first-come-research.md` | Can we count a campground's first-come sites? Partly |
| `docs/scanning-design.md` | Two-tier scanning, pacing rules, on-demand guards |
| `docs/reserveamerica-handoff.md` | RA endpoints (working), and the GoingToCamp blocker |
| `docs/reserveamerica-clients.md` | Which agencies run on ReserveAmerica |
| `docs/goingtocamp-clients.md` | GoingToCamp rec-area IDs, endpoints, availability codes |
| `docs/usedirect-clients.md` | UseDirect leads, unverified |
| `docs/campsage-ui-notes.md` | What to borrow from CampSage, and what to refuse |

## Quick start

```bash
# .venv already exists with camply installed; recreate only if needed:
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml       # edit home_base + sources
cp .env.example .env                     # notification targets; never committed

python3 scripts/manage.py catalog-refresh
python3 scripts/manage.py scan-once
```

Steps 1–3 run on the **standard library alone** — the tests need no
dependencies at all:

```bash
python3 -m unittest discover -s tests -t . -v     # 171 tests, no network
```

To see the whole pipeline with no deps and no network, point a config at the
built-in `Mock` provider (uncomment the Mock source in `config.example.yaml`).

## CLI

```
manage.py list-providers                  # what's wired up
manage.py catalog-refresh [--write-seed]  # enumerate + diff the catalog (§8k)
manage.py scan-once                       # one availability cycle + alerts (§8)
manage.py search <query> [--state OR]     # search the CATALOG, not search hits
manage.py map [--state OR]                # every pin + its honest status
manage.py backfill-coordinates [--write-seed]   # locate parks a provider can't
manage.py add-watch <name> [...]          # create a watch (§8b)
manage.py list-watches [--all]
```

## Layout

```
app/
  providers/  base.py  mock.py  camply_provider.py  reserveamerica.py  __init__.py
  db.py  store.py  catalog.py  scanner.py  notifier.py  config.py  util.py
  pacing.py                 # the one rate limiter every request goes through
scripts/manage.py
tests/test_core.py
data/seed/pnw_campgrounds.json
samples/                    # third-party reference repos — gitignored, read-only
```

## Alerts

One notification per watch per scan cycle when a cycle turns up more than one
opening — a digest listing every spot with its booking link — instead of one
buzz per campsite. A single opening still sends a single plain alert.

This deviates from the letter of §8b, which specifies batching only for
autonomous watches. In practice a targeted watch routinely matches a dozen
site-nights at once (2 sites × 6 nights in the mock run), so per-site alerts
produced exactly the ping storm §8b's batching exists to prevent.

Watches declared in `config.yaml` are **seeds** (§8c): inserted once if no watch
of that name exists, then never touched again — so pausing or editing a watch in
the app isn't silently reverted by the config file on the next run.

## The one invariant worth knowing

**The catalog never silently shrinks** (§8k). The map is drawn from the
`campgrounds` table — the known universe — with availability layered on top, so:

- a provider outage marks pins `stale`, it does not empty the map;
- a campground a live enumeration drops is **kept and flagged**, not deleted;
- a campground with no coordinates shows as "location unknown" and is excluded
  from distance filters, rather than dropped;
- search runs against the catalog, so a full campground is still findable.

This exists because Reehers Camp Horse Camp had bookable sites and was
*entirely absent* from CampSage's map. `tests/test_core.py` asserts each of
these directly.

## Being a polite client

recreation.gov and ReserveAmerica rate-limit and block aggressive callers (§13).
Keep `scan_interval_minutes` at 30 (never below ~10) and use a descriptive
User-Agent. Staggering and backoff are no longer left to good intentions:
`app/pacing.py` enforces them for the whole process, and the on-demand path
will share the same budget, so extra viewers queue rather than burst. Only the
scanner talks upstream — the UI reads SQLite.

## Note on `samples/`

`samples/` holds third-party reference projects (camply, CampSage page source,
ReserveAmerica and PerfectMind scrapers). It is **reference-only and gitignored**
— separate licenses, and it would bloat the repo. Read it; don't vendor it.

## A correction to the build plan

§6 and §12 state that camply's `CAMPSITE_SEARCH_PROVIDER` dict is keyed by
search-class name (`"SearchRecreationDotGov"`). Against camply 0.34.2 in
`samples/camply-main/` it is built as
`{provider.provider_class.__name__: provider …}` (`camply/search/__init__.py:57`),
so the keys are **provider** names: `"RecreationDotGov"`, `"GoingToCamp"`, etc.
`CamplyProvider` accepts either spelling and normalizes. Related: the plan says
21 camply providers; this version exposes 19.
