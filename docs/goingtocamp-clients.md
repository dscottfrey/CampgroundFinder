> **STATUS 2026-07-28: built and working.** `app/providers/goingtocamp.py`
> talks to the API directly — not through camply, which is broken (see below).
> Washington: 79 campable parks, 78 with coordinates. BC Parks: 114 campable,
> but the platform publishes coordinates for only one of them.
>
> **The endpoints that matter**, all verified live:
>
> | Purpose | Call |
> |---|---|
> | Full directory for a rec area | `GET /api/resourceLocation` — one request, carries name, `gpsCoordinates`, `rootMapId`, `resourceCategoryIds` |
> | Availability matrix | `GET /api/availability/map?mapId=…&getDailyAvailability=true` — every site x every night in one request |
> | ~~Park detail lookup~~ | `GET /api/maps` — **don't**; returns 6 org-level maps with `resourceLocationId: null`. This is what breaks camply. |
>
> A park's **root map usually holds no sites** — only `mapLinkAvailabilities`,
> the loops inside it, which must be followed. Alta Lake has four, so one park
> costs ~5 requests per window.
>
> **Daily availability codes** (documented nowhere; derived by running both
> query modes over one window and cross-tabulating):
> `0` = open that night, `1` = taken, `4`/`5` = some other state seen on 2 of
> 46 sites and never treated as open. A stay of N nights needs the first N
> codes to be `0`; the entry after the last night is checkout day and is
> ignored.
>
> Campable locations are those whose `resourceCategoryIds` include any of
> `-2147483648` (camp site), `-2147483647` (overflow), `-2147483643` (group).
> Washington lists 167 locations; only 79 pass that filter.
>
> Getting past the Azure WAF: see `docs/scraping-policy.md`.

---

# GoingToCamp (Tyler Technologies) — client systems

Two sources: a research note supplied by Scott 2026-07-27, and camply 0.34.2's
own `camply/providers/going_to_camp/rec_areas.py`. **Where they disagree,
camply wins** — it is working code with rec-area IDs, and its host list is
corroborated by CampSage's live booking links.

## The authoritative list (camply 0.34.2, 17 areas)

Each needs its `recreation_area_id` to be queried at all — there is no blind
enumeration call.

| Host | Area | ID | Region |
|---|---|---|---|
| `washington.goingtocamp.com` | Washington State Parks | **3** | WA |
| `tacomapower.goingtocamp.com` | Tacoma Power Parks | **6** | WA |
| `camping.bcparks.ca` | BC Parks | **12** | BC |
| `reservation.pc.gc.ca` | **Parks Canada** | **14** | CA-NAT |
| `wisconsin.goingtocamp.com` | Wisconsin State Parks | 7 | WI |
| `parkreservations.maryland.gov` | Maryland State Parks | 9 | MD |
| `midnrreservations.com` | Michigan State Parks | 17 | MI |
| `manitoba.goingtocamp.com` | Manitoba Parks | 15 | MB |
| `novascotia.goingtocamp.com` | Nova Scotia Parks | 13 | NS |
| `parcsnbparks.ca` | New Brunswick Provincial Parks | 16 | NB |
| `nlcamping.ca` | Newfoundland & Labrador Provincial Parks | 11 | NL |
| `reservations.ncc-ccn.gc.ca` | Gatineau Park | 10 | ON-QC |
| `ahtrails.ca` | Algonquin Highlands | 8 | ON |
| `longpoint.goingtocamp.com` | Long Point Region | 1 | ON |
| `stclair.goingtocamp.com` | St. Clair Region | 2 | ON |
| `maitlandvalley.goingtocamp.com` | Maitland Valley | 4 | ON |
| `saugeen.goingtocamp.com` | Saugeen Valley | 5 | ON |

## The big find: Parks Canada needs no custom provider

Build plan §4d says Parks Canada is *"not in camply → custom provider"*. **That
is wrong.** `reservation.pc.gc.ca` is GoingToCamp rec area **14** in camply.
Along with BC Parks (12), that means **both Canadian sources Scott asked for
are config entries, not scrapers.** Two custom providers just disappeared from
the roadmap.

## Three errors in the research note

| Note claims | Reality | Evidence |
|---|---|---|
| Missouri State Parks on GoingToCamp at `mostateparks.goingtocamp.com` | **UseDirect**, not GoingToCamp | camply lists `MissouriStateParks` under `search_usedirect.py`; CampSage books Missouri at `icampmo.usedirect.com/MSPWeb/` |
| Ohio DNR on GoingToCamp at `ohiodnr.goingtocamp.com` | **UseDirect**, not GoingToCamp | camply lists `OhioStateParks` under `search_usedirect.py`; CampSage books Ohio at `www.reserveohio.com/OhioCampWeb/` |
| Maryland at `maryland.goingtocamp.com` | Right platform, **wrong host** — `parkreservations.maryland.gov` | camply rec area 9 |
| Manitoba at `manitobaparks.goingtocamp.com` | **`manitoba.goingtocamp.com`** | camply rec area 15 |

Washington and Wisconsin in the note both check out.

The Missouri and Ohio mix-up is worth remembering: GoingToCamp and UseDirect
are *different platforms* that both host state park systems, and a portal's
branding does not tell you which one it is. Check camply's module before
assuming.

## Priority for us

1. **Washington State Parks (3)** — the WA gap; ReserveAmerica cannot fill it
2. **BC Parks (12)** — Scott asked for it
3. **Parks Canada (14)** — now free, given the above
4. **Tacoma Power (6)** — small, same state, cheap once WA works

## Blocker

All of the above is moot until the enumeration call works. Currently
`find_campgrounds(rec_area_id=[3])` raises `KeyError: -2147483647`, which is
Alta Lake State Park's real id — camply reaches the platform and then fails an
internal lookup. Full detail, plus the direct-endpoint fallback taken from
CampSage's page source, is in `reserveamerica-handoff.md`.
