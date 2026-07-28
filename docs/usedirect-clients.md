# UseDirect — client systems (leads for later discovery)

Two unverified research notes plus what camply and CampSage actually show.
**None of the county entries below have been checked by us.** Recorded so the
leads aren't lost, not as facts.

## County / municipal clients (research note, Scott 2026-07-27, unverified)

All California. Portal pattern is `<agency>.usedirect.com/<Name>Web/`.

| Agency | Portal | Scope claimed |
|---|---|---|
| Orange County Parks | `oc.usedirect.com/OrangeCountyWeb/` | regional and wilderness parks, campgrounds, beaches, parking passes |
| San Bernardino County Regional Parks | `countyofsanbernardino.usedirect.com/SanBernardinoWeb/` | regional parks, lakes, RV campgrounds |
| Riverside County (RivCoParks) | `rivcoparks.usedirect.com/RivCoWeb/` | regional parks, historic sites, riverfront campgrounds |
| Kern County Parks and Recreation | `kerncounty.usedirect.com/KernCountyWeb/` | regional parks, lakes, county campgrounds |

**Relevance to us: low for now.** These are all California, and the
county/municipal sub-directive targets Oregon and Washington. Their value is as
*evidence of the pattern* — UseDirect is a county-park platform, so when an
OR/WA county portal is inspected, UseDirect is a likely answer and one of the
first things to test for.

## The URL pattern is corroborated

The `<agency>.usedirect.com/<Name>Web/` shape matches what camply and CampSage
independently show, which is mild support for the list being real:

- camply hardcodes `oregonrdr.usedirect.com`, `azrdr.usedirect.com`,
  `floridardr.usedirect.com`, `icampmo1.usedirect.com`, `reservemn.usedirect.com`,
  `fairfax.usedirect.com`, `maricopardr.usedirect.com`, and others
- CampSage's map page books Missouri at `icampmo.usedirect.com/MSPWeb/`

So a quick platform test for any unknown county portal: look for a
`usedirect.com` host or a `/…Web/` path in its booking widget's network calls.

## On the apparent disagreement

Scott flagged this as Gemini disagreeing with the earlier GoingToCamp note.
Worth being precise: **this list does not actually contradict the Missouri /
Ohio finding.** It never mentions Missouri or Ohio. It lists California county
agencies on UseDirect, which is compatible with everything else we know.

The open question from the GoingToCamp note stands unchanged: that note put
Missouri and Ohio on GoingToCamp, while camply's module layout and CampSage's
live booking links both put them on UseDirect. Two sources against one. Not
settled by this list either way — see `goingtocamp-clients.md`.

## How to settle any of it

Do not argue from notes. Load the agency's booking page, open devtools →
Network → XHR, and read which host the availability call goes to (§6c). That is
the only answer that counts. Add the host to the allowlist at that point, one at
a time.
