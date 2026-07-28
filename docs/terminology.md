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

## ReserveAmerica publishes access mode — in the Site type column

The park page's site table is:

    Site# | Loop | Site type | Max # of people | Equip length/Driveway
          | Amenities | Online availability

**"Site type" is the access mode**, and it is authoritative: Brooke Creek's
sites read `WALK TO`. **"Equip length/Driveway" empty is the same fact stated
twice** — a site no vehicle can reach has no driveway. At Reehers the drive-in
horse sites read `Back-In`; all 21 Brooke Creek sites are blank.

Two independent signals, both from the operator, both per-site. That is what to
filter a bikepacker search on — not the icon.

## Site type is not an equipment restriction

Ground-truthed by Scott, 2026-07-28: Beverly Beach C27 is listed `TENT SITE`,
and he has camped it **in a van**. So:

> **"TENT SITE" does not preclude a campervan or an RV.**

The site type describes the site's character, not what may park on it. The only
authoritative prohibition is an explicit "RV prohibited" in the site's own
**description**, which we do not currently fetch.

Consequence for filtering: **an equipment filter may inform, but must never
exclude.** Hiding every `TENT SITE` from a van owner would remove sites they
have literally slept in — the Reehers failure with a different cause. Until we
read the description, "can my van fit?" is *unknown*, and §8g says unknown is
shown, not filtered away.

## The driveway length is a FLOOR, not a measurement

A Beverly Beach manager told Scott directly: when the park went onto
ReserveAmerica they had to answer every question in the setup form, and had no
staffing budget to actually measure the sites. So most were entered at a
default.

The data bears that out exactly. On loop A, **21 of 24 sites read exactly
"20 Back-In"**, and:

| site | listed | actually |
|---|---|---|
| A01 | `20 Back-In` | **53 ft** |
| A15 | `15 Back-In` | 15 ft |

The genuinely short site was entered accurately; everything else got the
default. So the number is a **minimum** — the real site is that long *or
longer*, never shorter.

`fits_equipment(driveway, length_needed)` encodes it as three states:

| listing | verdict |
|---|---|
| blank | **False** — no driveway at all, no vehicle reaches it (the `WALK TO` sites). The one case we can honestly exclude. |
| listed ≥ needed | **True** — the floor already clears it |
| listed < needed | **unknown** — may be far longer, as A01 is. Never "no". |

A 40-foot rig asking about A01 must not be told "no". It fits.

## ReserveAmerica's site type icon is NOT access mode

Confirmed by Scott against the real campground, 2026-07-28: the sites in
**Brooke Creek Hike-In Camp at L.L. Stub Stewart** — named `HIKE 01`…`HIKE 21`,
in a loop with "Hike-In" in its own name — carry the **`rv` icon** on
ReserveAmerica, and cannot accommodate an RV at all.

So `_SITE_TYPE_ICON` must not be treated as an access or equipment signal. The
site *name* and the loop name were right where the icon was wrong.
