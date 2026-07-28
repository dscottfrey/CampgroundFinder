# ReserveAmerica / Aspira — known client agencies

Supplied by Scott 2026-07-27 from a research pass. **Only Oregon has been
verified by us**; treat every other portal and contract code below as a lead to
confirm, per the project rule that unverified data is probably wrong.

## Why this matters to the build

One provider unlocks every agency on the platform. The provider must therefore
be parameterized by **(host, contractCode)** and never hardcoded to Oregon:

| Agency | Host | Contract | Verified? |
|---|---|---|---|
| Oregon Parks and Recreation | `oregonstateparks.reserveamerica.com` | `OR` | **yes — 65 parks enumerated 2026-07-27** |
| Georgia State Parks | `a1.reserveamerica.com` | `GA` | no |
| (sample repo, US Forest Service) | `www.reserveamerica.com` | `NRSO` | no |

Everything else below has no confirmed host or contract code yet.

## Tier 1 — state park systems

- **Oregon Parks and Recreation Department** — full state park roster, yurts,
  coastal campgrounds. *Our priority; already working.*
- **Colorado Parks and Wildlife** — parks plus hunting/fishing licensing (IPAWS)
- **Georgia State Parks & Historic Sites**
- **New Jersey Division of Parks and Forestry**
- **Alaska State Parks** — 120+ public-use cabins and campgrounds
- **Connecticut DEEP** — state forests, shoreline, island campsites
- **Florida Forest Service** — 80+ campgrounds

## Tier 2 — county, regional and special district

- **Charleston County Parks** (SC)
- **Larimer County Natural Resources** (CO) — Carter Lake, Horsetooth Reservoir
- **NOVA Parks** (VA) — Bull Run, Pohick Bay
- **East Bay Regional Park District** (CA)
- **Inyo County Parks** (CA) — Eastern Sierra
- **Oconee County Parks** (SC) — Lake Keowee
- **Estes Valley Recreation and Park District** (CO)

This tier is the bridge to the county/municipal sub-directive: several counties
that look like they need bespoke scrapers are actually just ReserveAmerica
contracts. Check here before building anything new.

## Western-states priority

Scott wants western states first. From this list, in order:

1. **Oregon** — done
2. **Colorado Parks and Wildlife** — largest western system after OR
3. **Alaska State Parks**
4. **East Bay Regional Parks** and **Inyo County** (CA)
5. **Larimer County** and **Estes Valley** (CO)

## The Washington gap

**Washington does not appear on this list**, which matches what we found
independently: WA State Parks run on **GoingToCamp**, not ReserveAmerica. So no
amount of ReserveAmerica work will cover Washington — that gap can only be
closed by finishing GoingToCamp. See `reserveamerica-handoff.md`.

## Before adding any agency

1. Confirm the portal host and contract code by loading
   `https://<host>/campgroundDirectoryList.do?contractCode=<CODE>` and checking
   the page title names the right agency.
2. Add the host to the sandbox allowlist only at that point — one at a time.
3. Pace it the same as Oregon: one request at a time, 6+ seconds apart.
