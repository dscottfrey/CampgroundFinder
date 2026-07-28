# Can we count a campground's first-come sites? — research, 2026-07-28

Question: could we discover how many first-come sites a campground has, put it
in the committed seed, and surface it as *"no reservable sites available, but
this campground has 3 first-come sites that might be available"*?

Answer: **partly, and not for the campgrounds where it would matter most.**

## What RIDB gives us

`GET facilities/{id}/campsites` (no API key needed) returns a record per site
with `CampsiteReservable`, `CampsiteType`, and `TypeOfUse`. Sampled live:

| Facility | Flagged | Sites | Non-reservable | Their types |
|---|---|---|---|---|
| Castle Rock Campground | `first_come` | **0** | — | — |
| Stillwater Campground | `first_come` | **0** | — | — |
| Jackman Park Campground | `first_come` | **0** | — | — |
| BROKEN ARROW | `reservable` | 279 | 145 | 105 standard, 26 group, 14 management |
| DIAMOND LAKE | `reservable` | 244 | 15 | 1 standard, 14 management |

### Finding 1 — the 206 first-come campgrounds have no site inventory at all

Every facility RIDB flags non-reservable returns **zero** campsite records.
That makes sense: RIDB's site inventory comes from the reservation system, and
a campground that takes no reservations was never loaded into it.

So for exactly the campgrounds where "this place has 3 walk-up sites" would be
most useful, **the number does not exist in RIDB.** We know the campground is
first-come; we cannot know how big it is. Getting that would mean parsing
Forest Service / NPS pages per campground — unstructured, fragile, and a much
bigger job than this one.

### Finding 2 — mixed campgrounds are real, common, and countable

BROKEN ARROW has 279 sites of which **145 are not bookable online**, and 105 of
those are ordinary `STANDARD NONELECTRIC` pitches. That is a genuinely mixed
campground and precisely the case the `first_come_sites` field was added for.
DIAMOND LAKE, by contrast, has 15 non-reservable sites of which 14 are
`MANAGEMENT`.

### The trap: `CampsiteReservable=False` does not mean "first-come"

At Trillium Lake, 11 of the non-reservable sites are type `MANAGEMENT` — camp
host and staff pitches, not available to the public at all. Counting those as
walk-up sites would tell a camper three pitches "might be available" when they
are somebody's job. `TypeOfUse` doesn't separate them either; every one of them
reads `Overnight`.

`MANAGEMENT` is filterable. What is **not** knowable from RIDB is whether a
remaining non-reservable standard site is a walk-up, a seasonal closure, or one
simply never loaded into the booking system. RIDB never says "first come first
served" anywhere.

## What we can honestly say

Not *"3 first-come sites that might be available"* — we cannot support "first
come" or "available". What the data does support:

> **131 of 279 sites here are not bookable online.** Often these are walk-up,
> but recreation.gov doesn't say which — worth a phone call.

That is still a real improvement on a bare "full", which is what those
campgrounds show today. It converts a dead end into a reason to investigate,
without inventing a fact.

## If we build it

- A backfill, shaped exactly like `backfill-coordinates` — separate from the
  scan cycle, idempotent, provenance recorded.
- One request per facility, plus paging for large ones (BROKEN ARROW needed
  six). 545 facilities at 2s spacing ≈ 25–35 minutes, once.
- Populates `first_come_sites` (True where non-management non-reservable sites
  exist, False where none do) plus a count. `False` is a real answer here and
  worth recording — DIAMOND LAKE genuinely has none.
- Excludes `MANAGEMENT` always. Records the count as "not bookable online",
  never as "first-come available".
- The 206 first-come facilities stay unknown, and that stays visible rather
  than being quietly rendered as zero.

## Verify before trusting

`facilityID` as a **query parameter** is silently ignored — passing
`campsites?facilityID=232831` returns all 137,117 campsites in RIDB, not the
facility's 65. The nested path `facilities/{id}/campsites` is the one that
filters. Same class of silent-wrong-answer as the Strapi pagination in
`docs/bc-coordinates.md`; always check the returned total against what you
expect.
