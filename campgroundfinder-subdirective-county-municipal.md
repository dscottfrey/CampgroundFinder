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

## 4. Priority order

1. **Reuse what's built.** ReserveAmerica and Campspot are already planned — any
   OR/WA county or private park on those is *free coverage*; just catalog it.
2. **ActiveNet** — highest leverage for county/municipal coverage (many agencies).
3. **RoverPass** — decent county/RV coverage.
4. **Firefly / CampLife** — build on demand, when a specific park you want is on one.
5. **Bespoke county systems** — only if a county portal turns out to use no shared
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
