# Campfire bans — Washington State Parks

Found by Scott on 2026-07-31 while looking for something else, and it answers
the "campfires allowed" filter he asked for earlier the same day.

    https://parks.wa.gov/about/news-announcements/alerts

## Why this one is easy

Read on 2026-07-31: the page is **server-rendered HTML, in full, on one
page.** No JavaScript, no pagination, no API to reverse-engineer, no session.
Every park's alerts are in the markup whether or not the disclosure triangle
is twisted open — the triangles are presentation, not lazy loading.

So the whole Washington burn-ban picture is **one request**, which suits the
once-a-day cadence Scott wanted. Nothing about this needs a scraping
argument: it is a public notice page, published to be read.

## What it carries

**Burn ban levels**, per park, each with a posted date and the rule in plain
words:

| level | what it means |
|---|---|
| Level 2 | wood fires restricted to fire pits in designated areas |
| Level 3 | gas/propane stoves and fire pits only — **no charcoal or wood** |
| Level 4 | no open flames of any type |
| "No fires at any time" | year-round prohibition, not a seasonal ban |

Note the last one is a *standing rule*, not a fire-season restriction — many
marine and heritage parks carry it with dates from 2024 and 2025. Treating it
as a current emergency would be wrong.

**And nine other alert types**, several of which matter as much as the bans:

    Burn Ban · Notification · Boating and Moorage · Construction · General ·
    Shellfish · Winter Recreation Advisory · Water Closure ·
    Part of the Park is Closed · Park is Completely Closed

**`Park is Completely Closed` and `Part of the Park is Closed` should feed our
catalog status directly.** Nisqually is closed for construction; Sequim Bay is
closed 5 June–15 September; Larrabee's camping closes 15 September 2026 to 15
June 2027; Saltwater's campground is closed outright. Today we would happily
show those as `unknown` and let somebody drive there. That is a bigger honesty
win than the fire filter.

`Water Closure` is also live and specific — Anderson Lake and Columbia Hills
both have toxic algae alerts — which is the AQI/water-quality axis arriving
from an unexpected direction.

## What to be careful about

* **Park names don't match our catalog exactly.** The page says "Alta Lake"
  where we hold "Alta Lake State Park", and "Mt. Spokane Sno-Park" is a
  different row from "Mount Spokane State Park". Match deliberately and
  **report what didn't match** rather than dropping it — an unmatched park is
  a park whose burn ban we are not showing.
* **This is Washington State Parks only.** Not federal, not Oregon, not the
  national forests. A filter that quietly works in one state is the
  "works on federal sites" generalisation `docs/terminology.md` warns about,
  so coverage has to be visible.
* **A ban is dated, and dates are the whole point.** A Level 3 posted in July
  is current; a "no fires" posted in 2024 is a standing rule. Store the posted
  date and show it.
* **`parks.wa.gov` is not on the network allowlist yet.** Ask Scott, with the
  exact edit to `.claude/settings.local.json`, before building the fetcher.

## The interface point Scott was actually making

He got there by clicking through to a park, then to alerts, then twisting a
disclosure triangle per park — *"back to me searching"*. The friction is the
argument: a camper should see "no wood fires here this weekend" on the
campground itself, not go looking for it. Same reasoning as the forecast
belonging in the popup rather than on a separate page.
