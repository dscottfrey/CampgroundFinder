# Locating the BC parks — design note, not yet built

113 of the 114 BC Parks campgrounds in the catalog have **no coordinates**,
because the GoingToCamp platform does not publish them for that portal. They
are catalogued, searchable, and honestly flagged "location unknown" (§8k/§13) —
but they cannot appear on the map or take part in distance filtering, which is
most of what the app is for.

## What the platform actually gives us

Measured across the 114 campable BC locations, 2026-07-28:

| Field | Coverage |
|---|---|
| `localizedValues[].website` — canonical `bcparks.ca` page | **114 / 114** |
| `localizedValues[].drivingDirections` | 114 / 114 |
| `gpsCoordinates` | **1 / 114** |
| `streetAddress`, `city`, `googleAddress`, `region` | 0 / 114 |

Washington, by contrast, fills `gpsCoordinates` for 78 of 79. This is a
per-portal data-quality difference, not a bug in our client.

The useful find: **every BC park carries its own authoritative URL**, e.g.
`https://bcparks.ca/bamberton-park/`. So the operator publishes a canonical
page per park, and the slug is stable and machine-readable.

## Candidate sources, best first

1. **BC's open data catalogue.** The province publishes open datasets, and
   provincial park locations/boundaries are exactly the sort of thing that
   lives there. One download would give authoritative coordinates for every
   park with no scraping at all, and would be re-fetchable on a schedule.
   **Unverified — I have not checked whether the dataset exists**, because the
   catalogue host is not on the sandbox allowlist. Check this first; if it
   exists, the rest of this document is unnecessary.

2. **The `bcparks.ca` page per park.** 114 one-time requests at 6s ≈ 12
   minutes, paced by the shared limiter like everything else. Authoritative
   (it is the operator's own page) and we already hold every URL. More work
   than option 1 and needs a parser that will eventually break.

3. **Geocoding `drivingDirections`.** Rejected unless the first two fail.
   It produces *approximations*, and this project's rule is that a coordinate
   is either real or absent — never guessed (§13). A geocoded point that lands
   a camper 8 km down the wrong forest road is exactly the class of failure the
   honesty invariant exists to prevent.

## The mechanism it should use

A **backfill step, deliberately separate from the scan cycle** — coordinates
change approximately never, so this must not ride along with availability.

- `manage.py backfill-coordinates --provider GoingToCamp:BC`, run by hand or on
  a slow maintenance schedule; results written to the catalog and committed to
  the seed like any other enumeration.
- **Record provenance per coordinate.** This is the part that makes the
  mechanism trustworthy: a point from the province's own dataset and a point
  scraped from a park page are not the same claim, and a later maintainer must
  be able to tell them apart — and to distinguish both from a guess, which
  should never appear. That needs a column on `campgrounds` (source + fetched
  date), which does not exist yet.
- **Never downgrade.** A backfill that fails leaves the park unlocated; it must
  not overwrite a good coordinate with a worse one, and re-running must be
  idempotent — the same never-shrink rule the catalog already follows.
- Same shape applies to the one Washington park in this state, Sun Lakes.

## What is needed to start

The BC open data catalogue host on the sandbox allowlist, so option 1 can be
checked. If the dataset is there this is a small job; if it is not, it is
option 2 and a parser.
