# What CampSage's pages are actually good for

Mined from three saved pages in `samples/Campsage app/` on 2026-07-27. Scott
dislikes their interface, so this is a list of what to **beat**, plus a few
things worth taking.

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

## The interface itself

Scott's verdict is that it's poor, and the map screenshot backs the main
complaint: pins carry long text labels that overlap heavily at any zoom where
you can see a region. For our map (§8h), prefer plain markers with labels on
hover or click, and cluster at low zoom.
