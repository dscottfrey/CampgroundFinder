# Scraping policy — supersedes build plan §6c

Decided with Scott 2026-07-28, after Washington State Parks turned out to sit
behind an Azure WAF that 403s any client not shaped like a browser.

## The shape of the decision

This app **sends business to the platforms it reads.** Booking is a deep-link
hand-off (§8j-B) — we never take a payment, never hold inventory, never place a
reservation. A person who gets an alert and books a site is revenue the operator
would not otherwise have had, because they'd have given up on a full campground.
The data itself is public agency availability: Washington State Parks, BC Parks,
Oregon State Parks.

So this is not competition and not extraction. That framing is what licenses
the rest of this document. **It would not license taking a small developer's
work, or republishing their data, or anything that costs them a customer.**

## The operating rule

> Don't get banned.

Everything below is downstream of that, and the honest engineering point is
that **rate and consistency matter far more than identity**.

## What we do

- **One stable User-Agent per provider**, browser-shaped so that WAF rules
  keyed on `Mozilla/5.0` let a read through, with `CampgroundFinder/0.1` and a
  contact-ish note appended. We stay identifiable and blockable on purpose.
- **Never rotate.** camply's GoingToCamp provider randomises its User-Agent per
  request via `fake_useragent`. That is both dishonest and *worse* for the
  stated goal — per-request variation is itself a bot signal that WAFs score
  on. A single consistent agent at human pace is the quieter choice.
- **Pace as if we were being watched**: one request at a time process-wide,
  6s between hits on a host, enforced by `app/pacing.py` where it cannot be
  bypassed.
- **Stop dead on 403/429** and stay stopped for an hour. A refusal is honoured,
  not worked around by retrying.
- **Read-only, always.** No booking, no account creation, no writes.
- **Never redistribute.** The catalog is for this app's users, not a dataset to
  republish.

## What we don't do

- No rotating agents, no proxy pools, no residential-IP services.
- No solving JS challenges, no CAPTCHA services, no TLS-fingerprint spoofing.
  **If a platform needs more than a stable browser-shaped header, that is a
  real no and we take it as one** — at that point we would be maintaining an
  evasion rather than fixing a header, which is both a bad use of time and past
  the line.
- No scraping small operators' own sites. The leverage principle in the
  county/municipal sub-directive says build platforms, not counties; that also
  keeps us on large platforms rather than someone's hand-built booking page.

## Why §6c said otherwise

Build plan §6c says "use an honest, descriptive User-Agent, do not rotate or
fake it." That is standard scraping-etiquette advice, and its good half —
**don't rotate, stay identifiable, stay accountable** — is kept above.

Its weak half is treating a browser-shaped string as dishonesty. Every browser
UA is already a compatibility fiction (`Mozilla/5.0 … like Gecko … Chrome …
Safari`); it is not an identity system. And in practice the rule inverted its
own purpose here: the fully honest agent was the only thing that got us
blocked, while the WAF let anything browser-shaped through. It was sorting
"declared itself a script" from "didn't" — not polite from abusive.
