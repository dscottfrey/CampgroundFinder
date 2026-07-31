# Per-user preferences

Scott, 2026-07-31: since there's a login anyway, preferences should live with
the account rather than in cookies that vanish.

## Short answer: yes, easily — and half of it already exists

The build plan's `users` table (§ schema) already carries what he suspected
we'd need:

```sql
role   TEXT,   -- 'admin' | 'friend'
status TEXT,   -- 'pending' | 'approved'
```

So **admin and basic levels are already in the design**, along with an admin
approval queue. Nothing new to invent there.

What's missing is one column:

```sql
prefs TEXT,    -- JSON blob, per-user UI preferences
```

A JSON blob rather than a column per preference, because these are read and
written whole, only ever by the owner, and are never queried across users. At
tens of users, on SQLite, this costs nothing.

## What goes in it

**1. Theme — light / dark / auto.** Store the **tri-state, not the resolved
value.** "Auto" means "follow the browser", and it has to survive as its own
setting; collapsing it to whatever the browser said at save time silently
converts a preference into a snapshot. See the theme split in
[`campsage-ui-notes.md`](campsage-ui-notes.md).

**2. Filter bar composition** — which filter chips this user wants in the bar
at all. Someone who never camps with a trailer shouldn't spend bar width on
rig length.

**3. Which of those displayed chips are enabled, persisted between logins.**
Scott clarified this on 2026-07-31: there is no separate "authored default"
competing with a remembered state. **The last state a user left the bar in
*is* their default** — it's one setting, written when they change a filter and
read back at their next login. Someone who always camps with a dog leaves the
dog filter on once and never thinks about it again.

That means the two settings are just *which chips exist for me* and *how
they're currently set*, and they can't contradict each other. A chip removed
from the bar simply stops applying; its stored value can stay in the blob and
come back if the chip is re-added.

Still worth a **"clear all filters"** control, but as an everyday convenience,
not as arbitration between two kinds of default.

**4. Where the map opens — region and zoom.** Scott, 2026-07-31, after
clicking CampSage's compass control and getting a browser geolocation prompt:
centring on your current location is worthless for trip planning, because when
you're planning you're at home, and when you're away you're already there. And
it asks for personal data to do it, which this project doesn't do.

The replacement is a stored map position: **a default region/zoom, or the last
one the user was looking at** — same one-setting shape as the filters above.
Someone who only ever camps in the Cascades should never pan there again.

## The parts that actually need care

* **First paint — solved by onboarding** (Scott, 2026-07-31). Rather than
  defending the map against unset preferences, arrange that it can never be
  reached with them unset:

  1. The first page anyone sees is the **login page**.
  2. Once approved, if a `preferences never chosen` flag is set (his example
     label — the stored flag can be whatever, e.g. `prefs.onboarded`), the
     next page is a **"set up your preferences"** page. It's the ordinary
     settings page with an extra header, and it requires the preferences to
     be set.
  3. Closing it goes to the map.

  **The user never sees an unset-up map.** Every later visit has a known
  preference by definition, so the server inlines the resolved theme into the
  initial HTML and there's nothing to flash.

* **Logged-out and pre-approval states.** The login page and a `pending`
  user's page render before any preference exists — they're the only screens
  that get the browser default, and neither is the map.
* **Cookies don't disappear entirely.** They stop being the *source of truth*.
  Something still has to carry the session.

## Scale

Tens of users. No preference sync conflicts worth solving, no caching layer,
no migration tooling — one JSON column and a settings page.
