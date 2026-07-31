# CampgroundFinder — Sub-Directive: County / Municipal / Private-SaaS Reservation Sources (OR + WA)

**What this is.** An addendum to the main build plan (`campgroundfinder-build-plan.md`),
extending **§4d (data sources)** with the *non-state, non-federal* campsite-reservation
systems used across Oregon and Washington — county & municipal parks, and the
third-party SaaS platforms that power most of them. Everything in the main plan
still governs: the **inclusion rule** (§4 — a source needs a real availability
feed), the **Provider interface** (§5), **scraping architecture** (§6c), **catalog
completeness** (§8k), **three-state filtering** (§8g), **read-only booking
hand-off** (§8j-B), and adding hosts to the sandbox allowlist **only as each
provider is actually built**.

---

## The leverage principle (read first)

Just like ReserveAmerica in the main plan, **the SaaS platforms are the
high-value targets: one provider per platform unlocks every campground on it.**

A county's own "reserve a campground" page almost always *sits on top of* one of
these platforms. So the first task for any county portal is **not** "write a
scraper for this county" — it's **identify which platform it runs on** (inspect the
booking widget's network calls, §6c). If it's a platform you've already built
(ReserveAmerica, Campspot, ActiveNet…), you write **no new code** — you just add
that campground to the catalog. **Build platforms, not counties.**

**All of these are custom providers** — none are camply-supported. Each follows the
§6c pattern: find the internal JSON availability endpoint via devtools, replay the
*read* calls with `httpx`, classify/filter locally, stay **gentle** (they
rate-limit), and keep booking as a **deep-link hand-off** (§8j-B). Each must pass
the §4 inclusion rule (a real availability feed) before it's worth building.

---

## 1. Third-party reservation SaaS platforms

Build **one provider per platform**. Each `Provider` gets a `platform` identity and
reads every campground on that platform you care about.

| Platform | URL | Powers | In main plan? | Notes |
|---|---|---|---|---|
| **ReserveAmerica / Aspira** | `reserveamerica.com` | State parks + many county/municipal parks | **Yes (§4d)** | Already the priority custom provider — note it *also* serves county/city parks; reuse the same provider for those. |
| **Campspot** | `campspot.com` | Private + some public/municipal campgrounds | **Yes (§4d)** | Already planned; reuse for any county/private park on it. |
| **ACTIVE Network / ActiveNet** | `active.com` (agency portals typically on `activecommunities.com`) | Many cities & counties (parks + rec facilities) | New | Big municipal rec-management SaaS; booking portals are per-agency ActiveCommunities sites. **Highest leverage** for city/county coverage. |
| **RoverPass** | `roverpass.com` | RV parks + some county/city campgrounds | New | Reservation SaaS; per-park booking pages. |
| **Firefly Reservations** | `fireflyreservations.com` | Mostly private RV parks/campgrounds | New | Smaller footprint; build if a park you want uses it. |
| **CampLife** | `camplife.com` | RV parks / campground management | New | Similar to Firefly; build on demand. |

For each **new** platform: confirm it exposes availability (inclusion rule),
reverse the JSON availability endpoint (§6c), add an enumeration path if the
platform has a searchable directory (for catalog completeness, §8k), tag `state`
per campground, and add its host(s) to the sandbox allowlist **when you start it**.

---

## 2. Oregon county / municipal portals — identify the platform first

Each of these is a *destination*, not necessarily its own system. **Step 1 for
each: open the reserve page, inspect the booking widget's network calls (§6c), and
identify the underlying platform** (one of §1, or a bespoke system). Then either
reuse that platform's provider, or — if genuinely bespoke — treat it as a new
custom provider.

| County (OR) | Reserve URL | Underlying platform | Action |
|---|---|---|---|
| **Columbia County Parks** | `columbiacountyor.gov/departments/ParksForestsRecreation/reserve-a-campground` | TBD — inspect | Map → reuse or build |
| **Douglas County Parks** | `yourdcparks.com` | TBD — branded portal, likely a SaaS | Map → reuse or build |
| **Jackson County Parks** | `jacksoncountyor.gov/departments/parks/reservations/index.php` | TBD — inspect | Map → reuse or build |
| **Tillamook County Parks** | `reservations.co.tillamook.or.us` | TBD — the `reservations.` subdomain suggests a hosted platform | Map → reuse or build |

*Washington county/municipal portals aren't enumerated here (Gemini's sweep covered
OR). When you extend to WA counties, follow the same "identify the platform first"
step — most will land on the §1 platforms.*

---

## 3. How these fit the existing architecture

- **Inclusion rule (§4):** each source needs an availability feed (reservation
  availability, or a first-come status). Private-RV SaaS all show availability
  calendars → they qualify. Confirm per platform.
- **Provider interface (§5) + registry:** one provider per **platform**,
  `kind='campsite'`, region-tagged. County portals resolve to a platform, **not**
  their own provider (unless truly bespoke).
- **Scraping (§6c):** internal JSON endpoints, not HTML/Selenium; **fetch broad,
  filter locally**; read-only + gentle.
- **Catalog completeness (§8k):** if a platform has a directory/search, enumerate
  it so those campgrounds are in the catalog (never "silently missing"); otherwise
  seed the specific parks you know. County parks a platform hosts still show up as
  catalog pins with an honest status.
- **Booking (§8j-B):** deep-link hand-off to the platform's own booking page. No
  in-app booking, no payment.
- **Credentials (§6b):** these are **public availability reads** — no login needed.
  If any platform requires an account just to *read*, apply the §6b rule (one-time
  service credential OK; ongoing/refreshing auth → reconsider including it).
- **Allowlist:** add each platform's host(s) to the sandbox allowlist **only when
  you start building that provider** — one understandable addition at a time.

---

## 3b. PacifiCorp is on CampLife (Scott, 2026-07-31)

A real reason to move CampLife up from "build on demand". **PacifiCorp** — the
utility — runs campgrounds in our region and books them through CampLife:

    https://www.camplife.com/1011/reservation/step1

`1011` looks like an org id in the path, which would make other operators
`/<orgId>/…` on the same platform — the same leverage shape as GoingToCamp's
rec-area numbers.

**Status: lead, not a finding.** The URL serves a JavaScript app shell, so
nothing can be learned by fetching the HTML — no endpoints, no park list, no
identifiers. It needs the §5.1 treatment: **open it and watch the booking
network calls.** Two ways to get there:

* Scott opens devtools, books nothing, and pastes the XHR/fetch URLs; or
* `camplife.com` goes on the network allowlist and the calls get probed here.

Until one of those happens, nothing about this platform should be written down
as fact — including whether `1011` means what it looks like.

**First look, 2026-07-31** (host allowlisted by Scott): the page returns 200
and is a webpack app — `clientApp/main.js`, `1865.js`, `2404.js` — so the API
is discoverable by reading those bundles.

**But it is behind an AWS WAF challenge**
(`…sdk.awswaf.com/…/challenge.js` loads before anything else). That is
bot-protection, and it changes the risk calculus rather than merely the
difficulty. Under [[campgroundfinder-scraping-policy]] the rule is *don't get
banned*, and a WAF is the operator saying plainly that automated clients are
unwelcome — solving its challenge would be evasion, not politeness, which is a
line this project has not crossed for any other source.

**So before any code is written here, the question for Scott is a policy one,
not a technical one:** is a WAF-protected platform in scope at all? Options, in
increasing order of nerve:

1. **Catalog only, no availability.** Enumerate PacifiCorp's parks once, by
   hand or from a public list, and show them with `unknown` status and a
   deep link. Nothing automated ever touches CampLife. This alone beats
   CampSage, which shows nothing at all (docs/campsage-ui-notes.md).
2. **Ask PacifiCorp.** A utility running public campgrounds may simply say
   yes, which no amount of clever fetching can substitute for.
3. **Read the JSON API directly** if the challenge turns out not to gate it.
   Worth *testing* to know, but only acting on with Scott's explicit call.

## 4. Priority order

**Revised 2026-07-31: CampLife moved from last to first in this group.** The
original ordering ranked platforms by how many *agencies* they serve. Scott's
correction is that PacifiCorp on CampLife is **a large number of highly
desired Washington sites** — and desirability is the thing worth optimising,
not agency count. A platform serving twenty agencies nobody wants to camp at
loses to one serving the reservoirs people actually book.

1. **CampLife** — PacifiCorp's Washington campgrounds (§3b). Top of this group
   on desirability, not breadth. Needs the network-call inspection first.
2. **Reuse what's built.** ReserveAmerica and Campspot are already planned — any
   OR/WA county or private park on those is *free coverage*; just catalog it.
3. **ActiveNet** — highest leverage by agency count for county/municipal.
4. **RoverPass** — decent county/RV coverage.
5. **Firefly** — build on demand, when a specific park you want is on one.
6. **Bespoke county systems** — only if a county portal turns out to use no shared
   platform at all.

---

## 5. Task list for Claude Code

1. For each **county portal** in §2: open it, inspect the booking network calls
   (§6c), and record which **platform** it uses in a small mapping table
   (`county → platform → parkId`).
2. **Group counties by platform.** For any platform already built
   (ReserveAmerica / Campspot), **add those parks to the catalog/config — no new
   code.**
3. For each **new platform** with parks worth having, build a `Provider`
   (§5/§6c): availability read, enumeration if the platform offers a directory,
   region tags, deep-link booking, gentle rate limits; add its host to the
   allowlist at that point.
4. Keep everything under the main plan's rules — completeness (§8k), three-state
   (§8g), read-only, deep-link hand-off.
5. **Log coverage:** after building, note which OR/WA counties are now covered and
   which remain, so gaps stay *visible*, not silent (the §8k spirit).

---

*Sources for this list gathered via a separate search pass (Gemini), focused on
Oregon/Washington non-state, non-federal reservation systems. Platform assignments
for the four named counties are marked TBD on purpose — confirm each by inspecting
its live booking widget rather than guessing.*
