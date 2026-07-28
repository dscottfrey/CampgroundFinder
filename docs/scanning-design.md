# Scanning design — two tiers, one rate limiter

Decided with Scott 2026-07-27. This governs everything that talks upstream.

> **STATUS 2026-07-28: steps 1 and 2 are built.** The shared rate limiter is
> `app/pacing.py`; round-robin and progress recording are in `app/scanner.py`;
> the status row is `scan_status` in `app/db.py` and rides along in
> `/api/state`. Steps 3–5 (adaptive cadence, on-demand refresh, zoom priority)
> are still design only.

The hard constraint it exists to serve: **the app runs on Scott's home
internet, and that connection must never get blocked.** Slow is acceptable.
Wrong is not. About a dozen users, rarely more than one at a time.

## The two tiers

**Tier 1 — background sweep.** Continuous, unattended, covers everything in
`scan_regions`. Nobody is waiting on it, so it runs at whatever pace is
politest.

**Tier 2 — on-demand refresh.** Fires when someone zooms into a small enough
area, and only then. Refreshes just the parks in view so the person looking at
them gets current data.

Tier 1 keeps the map broadly honest. Tier 2 makes the bit you're staring at
accurate.

## What the sweep actually costs

Measured 2026-07-27, not estimated:

| Source | Cost | Full OR+WA sweep at 6s spacing |
|---|---|---|
| ReserveAmerica | 1 request per park per fortnight (the park matrix) | 65 OR parks ≈ **7 minutes** |
| recreation.gov | per campground, per month — no matrix | 546 campgrounds, materially heavier |
| RIDB directory | 1 request per 50 campgrounds | seconds; catalog only, runs monthly |

**This overturns the original "one park per hour" idea.** At that rate Oregon
would take 17 days to cover once. The measurements say a full ReserveAmerica
sweep can run **hourly** and still be a very quiet visitor.

So: **don't weight ReserveAmerica** — there's no scarcity to ration. Weighting
is only worth it where capacity is genuinely short, which is recreation.gov.
There, favour the places Scott actually camps (Oregon coast state parks, Mt
Hood, Deschutes) and let the rest come round slower.

## Pacing rules — apply to both tiers

- **One request at a time.** Never parallel, ever.
- **Round-robin across sources**, with a pause between rounds. Interleaving
  maximises the gap between consecutive hits on any single host, which is what
  rate limiters actually measure. Scott's suggestion, and better than
  finishing one provider before starting the next.
- **6 seconds** between requests for ReserveAmerica, 2 for RIDB.
- **Stop dead on 403 or 429.** Skip that provider for the cycle. Never retry
  into a block.
- **One shared rate limiter for the whole process.** Tier 1 and tier 2 draw
  from the same budget, so ten people clicking at once queues instead of
  bursting. This is the single most important rule here: it converts
  user-driven load from unbounded into bounded.

## Adaptive cadence

How often a cycle *starts* may follow activity. How fast requests go inside a
cycle may not.

- nobody active → a sweep every couple of hours
- someone active in the last 15 minutes → back to the normal interval
- the gap between individual requests never changes

Watch scanning stays at a steady cadence regardless, even overnight — that is
the alerting product, and it is a handful of requests.

## On-demand: the four guards

On-demand refresh is only safe because it is bounded. All four are required:

1. **Park-count ceiling.** Above N parks in view, don't fetch — tell the user
   to zoom in. A wide view is served from the database.
2. **Freshness check.** Skip any park checked within the last N minutes. Most
   zooms should trigger zero requests.
3. **Debounce.** Wait for the map to settle. Panning must not fire per frame.
4. **The shared rate limiter.** Above.

At 6s spacing, a view holding 8 parks takes about 50 seconds. That is what the
progress widget is for — see below.

**Zoom as priority, not just as trigger.** Recently-viewed areas move to the
front of the background sweep's queue. Places nobody has looked at in weeks
drift to the back. The pace never changes, only the order.

## Never fake freshness

Scott suggested accepting false positives on the wide view. **We don't need to,
and shouldn't.**

When data is old, say so. The status model already carries `stale` and
`unknown`, and every record has `last_seen`. "Last checked 40 minutes ago" is
honest, costs nothing, and is the entire premise of the project. A false
positive sends someone driving to a campground that was booked hours ago —
the same class of failure as Reehers vanishing from the map, just inverted.

Rule: **staleness is displayed, never hidden.**

## Telling the user what's happening

Slowness is fine; unexplained slowness is not. Every wait needs a progress
indicator in plain language — no jargon, no bare spinner:

> Checking 8 campgrounds near you — 3 done
> *Going slowly on purpose so the camping websites don't block us.*

And on stale data:

> Last checked 40 minutes ago

The scanner must record what it is doing — current provider, queue position,
whether it is backing off and why — so the interface has something true to
display.

## Build order

1. ~~Pacing and round-robin in `scanner.py`, with the shared rate limiter.~~
   **Done 2026-07-28.**
2. ~~Scanner status recorded to the database for the progress widget.~~
   **Done 2026-07-28.**
3. Background sweep with adaptive cadence.
4. On-demand refresh with all four guards.
5. Zoom-based queue priority.

Steps 1 and 2 are prerequisites for everything else and need no UI.

## How steps 1–2 landed

- **`app/pacing.py`** owns every gap. Spacing is keyed by **host**, not by
  provider, because that is what a rate limiter on the other end measures — two
  providers on one host share its budget for free. An unlisted host gets the
  **slow** default (6s), not the fast one.
- The gap is measured **from when the last response landed** to the start of
  the next request, so a slow response lengthens the gap and never shortens it.
- A single process-wide lock is held across each upstream call. That is the
  "one request at a time" rule made structural rather than conventional.
- **403/429 latches the host off for an hour** on the *shared* limiter, so an
  on-demand refresh cannot walk into a block the sweep just discovered. The
  design says "skip that provider for the cycle"; an hour is the same rule with
  a wider margin.
- **A unit of work is one campground**, where the source names campgrounds.
  That is the granularity the round-robin interleaves at. A source that names
  none stays one unit — asking a provider for less isn't always possible.
- When a block cuts a source short, **every unchecked park in its queue is
  marked `stale`**. "We didn't look" must never render as "there's nothing
  there" — the Reehers failure, inverted.
- Progress is a single `scan_status` row: state, provider, target, done/total,
  a display-ready `message`, and a `detail` giving the reason for a wait
  ("Waiting 6s before the next request to oregonstateparks.reserveamerica.com").
  `PACING_NOTE` — *"Going slowly on purpose so the camping websites don't block
  us."* — lives in `pacing.py` next to the numbers it explains.

**One thing to know before building step 4.** camply owns its own HTTP, so its
several internal requests per search cannot be spaced individually. The adapter
holds the process's request slot around the whole call, which stops *our* other
providers piling on, but camply's internal pacing is unverified. Worth checking
before recreation.gov carries on-demand traffic.
