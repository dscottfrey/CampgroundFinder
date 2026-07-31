# What CampSage's pages are actually good for

Mined from three saved pages in `samples/Campsage app/` on 2026-07-27. Scott
dislikes their interface, so this is a list of what to **beat**, plus a few
things worth taking.

## CampSage cannot see PacifiCorp at all (Scott, 2026-07-31)

The clearest coverage gap found so far, and it is a large one: **PacifiCorp's
campgrounds appear nowhere on CampSage's map.** They book through CampLife
(§3b of the county/municipal sub-directive), and CampSage evidently carries no
CampLife integration.

Why this matters beyond scoring a point: these are **highly desired Washington
sites**, not obscure ones, and their absence is invisible to a CampSage user —
the map simply looks complete. That is the same failure the completeness floor
(§8k) exists to prevent, and it is the strongest argument yet for **catalog
first, availability second**: a campground we know about and can only say
`unknown` for still beats one nobody is told exists.

Worth adding to the acceptance test in `tests/test_core.py` that already pins
the campgrounds CampSage misses — once CampLife enumeration exists to name
them.

## First, the negative result

**Their pages do not reveal any provider endpoint.** Every call on the site
page goes to their own backend:

```
GET  /camp/site/<slug>/weather      weather, loaded async after the page
GET  /camp/site/<slug>/later        additional availability, lazy-loaded
POST /camp/watch                    {email, cg_id, cg_name}
POST /camp/tip                      user-submitted tips
GET  /camp/pro/status?email=…       paid-tier check
```

So they cannot help with the ReserveAmerica calendar problem, and their
ReserveAmerica deep-links are plain `campgroundDetails.do?contractCode&parkId`
with no dates. Confirmed dead end — do not go back to these files for the data
layer.

## Worth taking

**1. Feed-based alerts — `cancellations.ics` and `cancellations.rss`.**
The best idea on the site. A calendar subscription and an RSS feed of openings.

Why it fits this project better than it fits theirs: feeds need **no email
address, no phone number, and no third-party push service.** Nothing about the
user leaves the machine. Given the standing rule that none of Scott's data goes
anywhere, this is the only alert channel with zero disclosure — and it costs
one endpoint each. Both should ship alongside Apprise (§11), not after it.

Per-user, per-watch feeds, on a secret URL, served over Tailscale.

**2. Async enrichment.** Weather is fetched *after* the page renders, not
inline. Matches the step-4 plan: the page should never block on an enricher.
Render the campground, fill in air quality, fire, and weather as they arrive,
and show `unknown` rather than an empty gap (§8g).

**3. Lazy "later dates".** The first screen shows near-term availability; the
rest loads on demand. Cheap way to keep the initial view fast over a slow home
connection.

**4. Booking-window honesty.** They tell the user how far ahead the source
takes bookings ("recreation.gov takes bookings out to about …"). That prevents
"why is there nothing in June" confusion. Worth copying — and it pairs with our
`unknown` state: beyond the booking window is `tbd`, not `full`.

**5. An embeddable single-campground widget** (`/camp/embed/<slug>`). Not
needed, but a neat shape for a future "pin this campground" view.

## Worth deliberately not taking

- **The Pro tier and Amazon affiliate links.** The plan already says to drop
  these (§8h). The page is full of `amazon.com/s?k=headlamp&tag=campsage05-20`.
  Everyone gets the full feature set here; nothing is upsold.
- **Email/SMS-first alerting.** Requires collecting contact details for every
  friend. Feeds and Apprise cover it without a contact database.
- **Cancellation-only framing.** Their whole product is "catch a cancellation",
  which is *why* a merely-full park can vanish from their map. We show the full
  catalog with honest status (§8k); cancellations are one event within that,
  not the organizing idea.

## Zoom as a scan priority signal (Scott's idea, 2026-07-27)

Scott noticed CampSage shows fewer campgrounds zoomed out and more zoomed in,
and suggested **availability discovery could be rate-limited by zoom level.**

Worth doing, because we have a hard scarcity problem: ReserveAmerica
availability costs one request per site with no park-level matrix, measured at
~3.5 minutes per park per fortnight and ~4 hours to sweep all 65 Oregon parks.
We cannot check everything, so *something* must choose the order.

**The version to build:** the viewport and zoom of recent map views feed a
**priority queue for the scanner**. Parks people actually look at get
re-checked sooner; parks nobody has viewed in weeks drift to the back. The
scanner's pace never changes — same one-at-a-time, same seconds between
requests — only the order does.

**The version not to build:** zoom or pan triggering an upstream fetch. That
breaks the rule in §6c that only the scheduled scanner talks upstream, and it
makes outbound traffic scale with how much people browse. A dozen friends
panning around on a Friday evening would produce exactly the correlated burst
that gets a home IP blocked. Same reasoning as the idle/active cadence
decision: user activity may change *what* and *when*, never *how fast*.

**Separately, for display:** zoom-based decluttering is worth copying on its
own merits. Cluster markers at low zoom, and don't render labels until the
pins are far enough apart to read — see below.

## The interface itself

Scott's verdict is that it's poor, and the map screenshot backs the main
complaint: pins carry long text labels that overlap heavily at any zoom where
you can see a region. For our map (§8h), prefer plain markers with labels on
hover or click, and cluster at low zoom.

## The campground popup (Scott, 2026-07-31)

From their popup for Haystack Campground (East Shore). **Note it's a
Recreation.gov campground** — worth checking whether each of these exists for
the other providers before designing around it, since a field that's federal-
only becomes another `unknown` to display honestly.

**Take:**

* **A thumbnail photo of the campground.** Does more than any amount of text
  to say whether somewhere is worth the drive.
* **Cell coverage** — but **not as "best carrier"**. "3.1/5 best carrier"
  carries no information: it doesn't say whose signal, so nobody can tell
  whether it's theirs. Show **per-carrier ratings, AT&T and Verizon
  specifically**, where a rating is available for them.
* **The weather forecast.** The single best thing in their popup — and
  **they don't do anything with it.** It's inert text next to the name.
  **Keep it in the popup** — a camper reading about one place wants to see the
  forecast right there — *and* use it elsewhere, per the next section. Showing
  it and acting on it aren't alternatives.

**Drop:**

* **The star rating.** "⭐ 4 (90)" is meaningless — aggregate stars on
  campgrounds measure how nice people felt, not anything a camper can act on.

### The same popup on a ReserveAmerica park confirms the split

Scott's second screenshot is The Cove Palisades State Park (Oregon State Parks
via ReserveAmerica). It has **no thumbnail** — just their logo in the empty
frame — while the Recreation.gov one above does. So photos really are federal-
only through the providers, and the same is true of the amenity list: Cove
Palisades shows "🚤 Boat moorage" but **not the boat/paddle launch that is
actually there**, which is a different thing and the one a paddler needs.

**Scott's answer to both: put it in our own master list.** If a photo can't be
scraped — or even where it can — the campground's photo and its features
belong in the catalog we maintain, not in whatever each provider chose to
publish. Same reasoning as the seed files being the completeness floor, and as
painting our own campground names on the map because neither basemap labels
them usefully: **where the providers are inconsistent, we own the field.**

Consequences worth writing down before building it:

* A curated field needs a **provenance stamp** like everything else here —
  "ours" vs "from the provider" vs `unknown` — so a missing photo reads as not
  yet added rather than as no photo existing.
* **Moorage and launch are separate features**, not one boating flag. Whatever
  the feature vocabulary ends up being, it has to distinguish them, along with
  hand-carry/paddle launch versus trailer ramp.

## Filters dim; they never hide (Scott, 2026-07-31)

The rule, stated plainly after more time with CampSage:

> **Always show a dot for every campground.** CampSage hides them entirely
> under some filters and at some zoom levels, and that is the thing to beat.

A dot per campground is an easy way to navigate, it shows **density**, and it
carries a lot of information for free. A filter's job is to tell you which
ones match — not to pretend the rest don't exist. This is the same honesty
rule as `unknown`, `stale` and first-come: shown with an honest status, never
quietly dropped.

**Names are a separate matter, and they're less important.** Showing them only
at higher zooms is right — the names aren't all that relevant, the dot is.

### The dim state

**50% opacity on the dot and its name tag**, as a starting value to look at
rather than a settled number. Triggered by any of:

* **No availability within the selected date range**
* **AQI filter**
* **Temperature filter** (outside the desired min/max)
* **Forecast filter** — raining, say
* **Amenities filter** — lacking a wanted feature: beach, launch, and so on

**The opacity is not cumulative.** Failing four filters looks exactly like
failing one. Dimness answers "does this match?", not "how badly?" — stacking
it would make a heavily-filtered map fade to nothing, which is hiding by
another route.

*(One thing to confirm with Scott: he wrote that the amenities filter "may add
a cumulative effect" for lacking several features, then that the opacity would
not be cumulative. Recorded as non-cumulative throughout, which is the reading
that keeps the rule simple — worth a sentence to confirm.)*

This is also **opacity doing real work as a meaning channel**, which matters
for the reasons in the encoding section below.

**Phone consequence:** a dim small dot on a busy topo map is the area problem
and the opacity problem hitting the same pixels. On a narrow screen, dim the
dot but **don't shrink it as well** — and check that a 50% dot is still
findable against forest green before trusting the number.

The forecast still shows in the popup where CampSage has it; showing it and
acting on it aren't alternatives.

Two things this needs, both already planned: the weather and AQI enrichers
(build plan step 4), and the per-user filter preferences in
[`user-preferences.md`](user-preferences.md) — a temperature range is exactly
the kind of setting nobody wants to re-enter every visit.

### Wanted: a "campfires allowed" filter

Scott asked whether fire-ban information can be had. **Not researched yet, and
not to be assumed.** Note it is a *different* dataset from the planned
wildfire enricher (build plan step 4, `fire_status`): that one answers "is
there a fire burning near here", where this answers "may I light one". A park
can be perfectly clear of smoke and still under a total burn ban.

Where it might come from, all unverified: USFS forest-level fire restriction
orders, Oregon and Washington state park announcements, county burn-ban
notices, and the ODF/DNR regulated-use closures. The likely problem is that
these are published as **prose announcements rather than a structured feed**,
which would make this a text-scraping and staleness problem rather than an
API one. Worth an hour of research before it goes on the plan.

**Update (Scott, 2026-07-31):** he pointed at
[firerisk.ai/burn-ban-map](https://firerisk.ai/burn-ban-map) — a burn-ban and
fire-risk map that drills down by state — and suggested it as a **"check once
a day or week" filter feeder.** The cadence instinct is right: these change on
the order of days, not minutes.

**But go to its sources, not to it.** Read on 2026-07-31, the site is an
*aggregator* of things that are already public:

* **NWS Red Flag Warnings and Fire Weather Watches** — `api.weather.gov`, a
  free documented API, no key.
* **NIFC federal land restrictions** — and
  `data-nifc.opendata.arcgis.com` **is already in our network allowlist**.
* **USFS Region 2, via ArcGIS.**

It exposes no API of its own, and its own disclaimer says it is unaffiliated
with any agency and that users must verify with local fire authorities. So
scraping it would add a dependency, a middleman's staleness, and unclear terms
in exchange for data we can fetch first-hand. Use it as a **reference for what
to gather**, not as the feed.

The multi-second draw he noticed is the tell: that is an **ArcGIS feature
service rendering client-side**, so the lag is a rendering problem, not a data
one. The same service answers a REST query with the polygons and attributes as
JSON, which is what a once-a-day job would use.

Two distinctions to keep, both the same discipline as everything else here:

* **A Red Flag Warning is a weather condition, not a rule.** It says
  conditions are dangerous; it does not say you may not light a fire. Merging
  the two would publish a prohibition nobody issued.
* **IFPL governs industrial operations (logging), not campers.** Scott saw a
  state drill-down to IFPL levels — worth confirming, since the page read
  didn't surface it. Either way it correlates with campfire restrictions
  without being one, and the field we actually want is the **public use
  restriction**. IFPL may be the easiest thing to find rather than the right
  thing to show.

## CampSage now ships a native iOS app (Scott, 2026-07-31)

Not a web app — on the App Store. Scott's read: running on a mobile device
would remove the bot-scraping barriers, and he may want to go the same way.
**Keep the option open in every design decision from here.**

### What a native client genuinely changes

* **Requests come from each user's own phone**, so load spreads across ~12
  residential IPs instead of one home connection. That is the open question
  already logged in the handoff for browsers, and a native app is the
  stronger version of it: twelve people checking campgrounds *is* what is
  happening, and it finally looks like it.
* **No CORS — but only for a genuinely native client.** Scott's point
  (2026-07-31): *"it's still all really a wrapped webview anyway"*, and he is
  right that most of these apps are. **A wrapped WebView is bound by the same
  origin rules as a browser**, so wrapping does not unlock provider endpoints
  — only native HTTP, or a native bridge behind the WebView, does. The CORS
  advantage is real but belongs to an architecture nobody has committed to.

  Worth noting the question dissolves anyway if the app talks to **our** API:
  same origin, no CORS, nothing to solve. CORS was only ever a problem for
  talking to providers directly.
* **It can present as an ordinary mobile client**, which several of these
  platforms serve first-class.

### What it does not change — the word "entirely" is too strong

* **A WAF challenge is still a WAF challenge.** AWS WAF's is JavaScript; a
  native app either embeds a WebView to run it or fails it. CampLife does not
  become reachable by changing transport.
* **Terms of service are unchanged**, and so is
  [[campgroundfinder-scraping-policy]]. The rule is *don't get banned*, and
  distributing requests across real users' devices is defensible precisely
  because it reflects real use. **Building a native client in order to defeat
  a bot barrier is evasion whichever wire the request goes down** — that is a
  policy line, and moving the code to a phone does not move the line.
* **A server is still required.** Watches and notifications have to run when
  nobody's phone is awake. This is a split, not a replacement.

### Scott's intended shape (2026-07-31)

An **iPhone and iPad app, allowed to run on Apple Silicon** — the "Designed
for iPad" path, which runs on Apple Silicon Macs unless explicitly opted out.
A full native Mac app is a later question, not this one.

**The thing to weigh before that, and it is not technical:** every pacing
decision in this project rests on *~12 users on one home connection*
([[campgroundfinder-deployment-and-pacing]]). **An App Store release is
unbounded.** If a thousand people install it, a thousand phones start querying
ReserveAmerica and GoingToCamp, and "don't get banned" stops being about our
IP and becomes about whether the *platform* notices a new source of traffic
shaped like an app.

That does not make it a bad idea — CampSage evidently ships one. It does mean
the two versions want different rules:

* **Private / TestFlight / a dozen friends** — today's pacing is fine, and
  distributing requests across devices genuinely helps.
* **Public App Store** — the client cannot be trusted to pace itself in
  aggregate. Availability would have to come from *our* cache, served by our
  API, with the server doing the upstream fetching at a rate we control. Which
  is the architecture we already have, and inverts the appeal: the phone
  becomes a nicer front-end, not a way around anything.

Worth deciding which of those it is **before** the client is built, because it
determines whether the app talks to providers or only to us.

### What to do about it now

Nothing, except **keep the boundary clean**: the web UI should talk to our own
JSON API rather than reaching into the database or rendering server-side HTML
with logic baked in. If that holds, a native client is a *second front-end on
the same API* rather than a rewrite — and the honesty rules (`unknown`,
`stale`, dim-never-hide) live in the API's responses where both clients
inherit them, instead of being reimplemented twice and drifting.

## Phones are a target (Scott, 2026-07-31)

Standing instruction: this will be used on mobile, so **every design decision
below has to state its phone consequence.** Where one is missing, it hasn't
been thought about yet. The ones known so far, from the notes in this file:

* **The one-row filter bar wraps on a phone.** This is the real reason
  CampSage has a hide-filters button, and it means our "keep the bar visible"
  rule needs a narrow-screen form — a collapsed bar is acceptable there, but
  only if it still shows what's active.
* **Floating corner panels eat a small viewport.** The legend especially; at
  phone width it wants to be a tap-to-open sheet rather than a permanent box.
* **The popup covers most of a phone screen**, thumbnail included. Bottom
  sheet rather than a floating card is the usual answer.
* **Label density.** 774 pills is a smear on a laptop and worse on a phone —
  the zoom threshold that hides labels has to be width-aware, not fixed.
* **Small marks lose their size and opacity differences** when the screen
  shrinks, which is the weather-opacity idea and the colourblindness area
  problem hitting the same pixels.
* **No hover.** Anything that reveals on hover has to have a tap equivalent.

## Map chrome — what to take and what to fix (Scott, 2026-07-31)

From a live screenshot of `campsage.app/camp/map`. Our map today is the
opposite shape: a fixed-height window sitting below a stacked header, control
row, base-map row, and legend (`app/static/index.html`). Theirs is worth
copying structurally even though the styling isn't.

**Take:**

* **Full-page map.** The map is the page, edge to edge, not a pane inside a
  document. Everything else floats over it.
* **Pill-shaped label backgrounds.** Rounded capsules behind the campground
  name, with the count in a contrasting round badge on the trailing end. Much
  more readable over topo tiles than our current painted text.

  **The principle behind their black pill is right; the black isn't.** Scott
  worked out why it works (2026-07-31): black appears nowhere on the basemap,
  so the pill can never be confused with terrain. That's the rule to keep —
  **the pill fill must be off the basemap's palette.** But he doesn't like the
  look and doesn't want the map to read as a direct copy, so we need a
  different off-palette answer.

  What's actually available is narrower than it looks. A topo basemap already
  spends green (forest), blue (water), tan/brown (terrain), off-white (open
  ground), and pale red-pink (roads) — and for Scott brown collapses into
  green and purple into blue, so those five occupy **green, blue, warm-pale,
  and near-white** between them. That leaves saturated warm hues and the
  neutral extremes genuinely clear.

  **Decided (Scott, 2026-07-31): the inverse of theirs — a near-white/cream
  pill with dark ink.** Where it overlaps the map's own white areas it is
  separated by the subtle drop shadow beneath it, not by fill colour, which is
  what makes the shadow load-bearing rather than decorative. It's also the
  option furthest from CampSage's look. Not final for all time, but it's the
  direction to build first.
* **One top bar that holds nav and filters together.** Search, date, and the
  filter toggles live in a single horizontal strip across the top, with the
  view switcher (Map / List) at the right end. Ours currently spends three
  stacked rows on the same job.
* **Floating controls elsewhere.** Zoom, legend, and share sit as small
  floating panels pinned to the corners rather than in the page flow.

  **Not the locate-me compass.** Scott clicked it (2026-07-31) and it fires a
  browser geolocation prompt to centre on where you are — **worthless in a
  trip-planning app unless you're already away from home**, which is the one
  time you aren't planning. It also asks for personal data to do it, which
  this project doesn't do. **Replace it with a remembered map position:** a
  default region/zoom, or the last region/zoom the user was looking at, so
  the map opens somewhere useful without asking anyone anything. That's a
  per-user preference — see [`user-preferences.md`](user-preferences.md).
* **Very subtle drop shadows.** Just enough to lift a pill or a panel off the
  terrain and make its edge legible without drawing a border. Soft, low
  opacity, small offset — the separation should be felt, not seen.

**Fix, don't copy:**

* **The Filters button that hides the filter bar.** Scott asked what it's for
  (2026-07-31) and the honest answer is: about 55px of vertical map, which is
  a real gain on a phone where the bar would wrap to several rows, and close
  to nothing on a desktop. It's a mobile pattern shipped to desktop unchanged.
  The cost is worse than the gain — **hiding the bar hides which filters are
  on.** With the remembered filter state in
  [`user-preferences.md`](user-preferences.md) that's the exact recipe for
  "why is this map empty", because the answer is sitting behind a button.
  Keep the bar visible; if it ever has to collapse on a narrow screen, the
  collapsed form has to still say what's active.

* **Wasted space at top centre.** Their header row is mostly empty between the
  logo and the right-hand nav. Ours should either use that space or not
  reserve it — one bar, packed.
* **Black overlays everywhere.** Every panel, pill, and legend on their map is
  a heavy dark slab, and it fights the map underneath. Use light or
  translucent surfaces so the terrain stays visible through the chrome.

**Respect the browser's dark/light preference across the whole palette**
(Scott, 2026-07-31). He rarely runs dark mode himself, but honouring
`prefers-color-scheme` is a point of respect for whoever does. Today only the
page chrome is theme-aware (`styles.css` line ~197 swaps `--bg`, `--card`,
`--ink`, `--muted`, `--line`); the five status colours and the painted pin
labels are deliberately not, on the stated grounds that map tiles stay light
whatever the page does. That reasoning still holds — both our basemaps are
light, and the `auto` button switches on *zoom*, not theme — so the split to
build is:

* **Chrome that sits over the map** — the top bar, floating panels, legend,
  and the label pills — follows the theme. In dark mode the cream pill
  inverts to the tinted-charcoal option from above, which is the same
  off-palette rule applied to a dark surface.
* **Anything that must read against the tiles themselves** — status marks,
  their halos — stays tuned to the light tiles regardless of theme, and the
  comment in the CSS should say so rather than leaving it to be rediscovered.

If a dark basemap is ever added, that split collapses and the whole thing
becomes theme-aware together; note it as the trigger.

**Treat that split as a guess, not a finding** (Scott, 2026-07-31): "I will
need to see how it breaks. It may be fine, it may not." Build the theme-aware
version, look at it in both modes, and let the breakage decide where the line
actually goes. The caution above is reasoning about a rendered page nobody has
rendered yet.

**On encoding status:** colour is fine to use — it just can't be the only
thing carrying the meaning. Shape is what we use today (open = filled circle,
full = square, and so on), but size, opacity, and animation are all available
too, and are often better on a busy map: a bigger, more opaque pill for "open
now" and a small, faded one for "not checked yet" reads at a glance without
anyone having to resolve a hue. Pick whichever channels suit the element;
just never ship one where hue alone is the difference.

**Three specifics from Scott's own vision (2026-07-31)** that this redesign
has to respect, because pills and floating panels are exactly where they bite:

* **Purple can't mean anything different from blue.** His red channel is
  desaturated and partly seen as green, so he sees purple as blue — always,
  with no exception. Purple is still perfectly fine to use; it just can't be
  the thing that says "this one is not the blue one". Brown and green are the
  same story. Our palette has `--available: #0b5cab` (blue) and
  `--stale: #7b52ab` (violet) doing exactly that job, so `--stale` needs a
  hue that actually differs — shape is carrying it today. Same check on every
  pill fill.
* **Pastels and tinted greys are hard to tell apart *as hues*.** He can see
  them perfectly well — he just can't say which colour they are. So this is
  not a constraint on the light, translucent panels above at all: their fill
  is decorative, it carries no meaning, and soft is fine. The rule only binds
  where a hue has a job to do. `--unknown: #d6d1c6` is a tinted pale grey
  that *is* carrying meaning, and that's the one to fix.
* **Check the palette as a whole, not pair by pair.** Colours collapse in
  sets of any size — a chart with six pastel lines can have all six read as
  one colour at once, and no amount of "well, those two differ" rescues it.
  The question is always "do all of these separate from each other", asked of
  the full set at the size they're actually drawn.
* **Area matters — coloured type and thin lines have almost no area** and are
  the worst possible carriers of colour. This is the real argument for the
  pill: a filled capsule gives the colour enough surface to be seen at all,
  where our current painted label text does not.
