# CampgroundFinder

A personal, self-hosted campsite availability tracker for the Pacific Northwest.
Scans reservation providers on a polite schedule, keeps a **complete campground
catalog** (not just search hits), and alerts you when a spot you want opens up.

Design and rationale live in [`campgroundfinder-build-plan.md`](campgroundfinder-build-plan.md).
Section references below (§5, §8k, …) point into it.

## Status — last updated 2026-07-27

| Step | Scope | State |
|---|---|---|
| 1 | Scaffold, providers, `store.py`, core tests | **done** |
| 2 | camply adapter | **done, verified live** against recreation.gov |
| 3 | `catalog.py`, `scanner.py`, `notifier.py`, `config.py`, `manage.py` | **done**; 546 real campgrounds seeded |
| 4 | Enrichers (AQI, wildfire, water, weather) + three-state filters | not started |
| 5 | FastAPI + Leaflet UI | **partial** — a stdlib server and list view exist; no map |
| 6 | Docker + Tailscale + multi-user | not started |
| 7 | PerfectMind provider | not started |
| 8 | ReserveAmerica | **Oregon done**; GoingToCamp blocked, others not started |

**77 tests, standard library only, no network required.**

### What actually works

- **546 real campgrounds** — 355 Oregon, 191 Washington — enumerated live from
  recreation.gov's RIDB directory with coordinates and reservable flags.
- **recreation.gov availability**, through camply. Verified: Trillium
  (campground 232831) returns real openings with booking links.
- **ReserveAmerica for Oregon** — all 65 state parks with coordinates, plus
  per-park availability. Reehers Camp Horse Camp included, parkId 412704,
  20 horse sites and 14 tent sites.
- **A web page** — `manage.py demo`, then http://127.0.0.1:8080. List view
  only; the map is not built.

### Where we left off — read this first

**Blocked: GoingToCamp.** This is the only route to Washington State Parks,
BC Parks, and Parks Canada — ReserveAmerica cannot cover any of them. camply
fails with `KeyError: -2147483647`, which is a *real park id* (Alta Lake State
Park), not a bug sentinel. A direct-to-endpoint fallback is documented, taken
from CampSage's page source. See `docs/reserveamerica-handoff.md`.

**Next build step, per `docs/scanning-design.md`:** pacing and round-robin in
`scanner.py`, which currently has **no throttling at all**. That matters before
any wider scanning happens, because this runs on a home connection that must
not get blocked.

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
python3 -m unittest discover -s tests -t . -v     # 77 tests, no network
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
  providers/  base.py  mock.py  camply_provider.py  __init__.py   # §5, §6
  db.py  store.py  catalog.py  scanner.py  notifier.py  config.py  util.py
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
Keep `scan_interval_minutes` at 30 (never below ~10), use a descriptive
User-Agent, stagger providers, and back off on 429/403. Only the scheduled
scanner talks upstream — the UI reads SQLite, so extra viewers cost zero
upstream calls.

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
