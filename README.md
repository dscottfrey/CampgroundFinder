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
| 3 | `catalog.py`, `scanner.py`, `notifier.py`, `config.py`, `manage.py` | **done**; 610 real campgrounds seeded |
| 4 | Enrichers (AQI, wildfire, water, weather) + three-state filters | not started |
| 5 | FastAPI + Leaflet UI | **partial** — a stdlib server and list view exist; no map |
| 6 | Docker + Tailscale + multi-user | not started |
| 7 | PerfectMind provider | not started |
| 8 | ReserveAmerica | **Oregon done**; GoingToCamp blocked, others not started |
| — | Pacing: shared rate limiter, round-robin, scanner status | **done** (`docs/scanning-design.md` steps 1–2) |

**121 tests, standard library only, no network required.**

### What actually works

- **610 real campgrounds** — 545 from recreation.gov's RIDB directory, plus all
  **65 Oregon state parks** enumerated live from ReserveAmerica on 2026-07-28,
  every one with coordinates. Reehers Camp Horse Camp is in there at
  45.7067, -123.3381.
- **recreation.gov availability**, through camply. Verified: Trillium
  (campground 232831) returns real openings with booking links.
- **ReserveAmerica for Oregon** — all 65 state parks in the committed seed
  with coordinates, plus per-park availability. Reehers Camp Horse Camp,
  parkId 412704, 20 horse sites and 14 tent sites. A source with an empty
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

**Blocked: GoingToCamp.** This is the only route to Washington State Parks,
BC Parks, and Parks Canada — ReserveAmerica cannot cover any of them. camply
fails with `KeyError: -2147483647`, which is a *real park id* (Alta Lake State
Park), not a bug sentinel. A direct-to-endpoint fallback is documented, taken
from CampSage's page source. See `docs/reserveamerica-handoff.md`.

**Next build step, per `docs/scanning-design.md`:** step 3 — the background
sweep with adaptive cadence, then step 4 (on-demand refresh, all four guards)
and step 5 (zoom-based queue priority). Steps 1 and 2 — the shared rate limiter
and the scanner status row — are done, so the pacing groundwork wider scanning
needed is in place.

One caveat carried forward: camply owns its own HTTP, so its several internal
requests per search can't be spaced individually. The adapter holds the shared
request slot around the whole call, but camply's internal pacing is unverified —
worth checking before recreation.gov carries on-demand traffic.

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
| `docs/scanning-design.md` | Two-tier scanning, pacing rules, on-demand guards |
| `docs/reserveamerica-handoff.md` | RA endpoints (working), and the GoingToCamp blocker |
| `docs/reserveamerica-clients.md` | Which agencies run on ReserveAmerica |
| `docs/goingtocamp-clients.md` | GoingToCamp rec-area IDs, incl. BC and Parks Canada |
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
python3 -m unittest discover -s tests -t . -v     # 121 tests, no network
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
