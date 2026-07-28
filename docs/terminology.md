# Terminology — two axes people keep conflating

Settled with Scott 2026-07-28, after the phrase "walk-up" appeared in UI copy
and turned out to mean opposite things to different campers.

## The two axes are independent

**How you book it**
| term | meaning |
|---|---|
| reservable | you can book it in advance, online |
| first-come, first-served (FCFS) | no reservation; you turn up and claim it |
| not bookable online | *what we can actually observe* — see below |

**How you reach it**
| term | meaning |
|---|---|
| drive-in / standard | you park at the site |
| walk-in / hike-in | you park elsewhere and carry your gear in — the backpacker and bikepacker case, including people arriving without a car |
| boat-in, equestrian, … | other access modes |

A campervan owner wants **FCFS + drive-in**. A bikepacker wants **hike-in**, and
may not care how it is booked. Those are different queries against different
fields, and a filter that mixes them is useless to both.

## Never say "walk-up"

It is read as an access mode ("walk in with your gear") by some and as a
booking mode ("walk up without a reservation", like walk-up ticket sales) by
others. Both readings are current and defensible, so the phrase cannot be used
in anything a camper reads. Say **"first-come, first-served"** for booking and
**"hike-in"** or **"walk-in"** for access.

## What we can actually observe

For federal campgrounds, RIDB gives `CampsiteReservable`. That supports exactly
one claim: **"not bookable online"**. It does *not* establish FCFS — the site
could equally be seasonally closed or never loaded into the booking system
(`docs/first-come-research.md`). So the stored field is the narrow, measured
one and the interface must not upgrade it.

Access mode appears to live in RIDB's `CampsiteType`, which
`inventory.classify_sites` now captures per campground so the question never
costs a second pass over every facility.

## ReserveAmerica's site type icon is NOT access mode

Confirmed by Scott against the real campground, 2026-07-28: the sites in
**Brooke Creek Hike-In Camp at L.L. Stub Stewart** — named `HIKE 01`…`HIKE 21`,
in a loop with "Hike-In" in its own name — carry the **`rv` icon** on
ReserveAmerica, and cannot accommodate an RV at all.

So `_SITE_TYPE_ICON` must not be treated as an access or equipment signal. The
site *name* and the loop name were right where the icon was wrong.
