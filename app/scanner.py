"""The scan cycle (§8) — verify availability over the catalog, alert on new.

Runs over CATALOGUED campgrounds, not over a search-hit list, so the map keeps
showing full / unknown / stale pins instead of quietly losing them (§8k).

**Pacing (docs/scanning-design.md).** A cycle is broken into units — one
campground each where the source names campgrounds — and the units are taken
**round-robin across sources**, one at a time, with a pause between rounds.
Interleaving is the point: it maximises the gap between consecutive hits on any
single host, which is what a rate limiter on the other end actually measures.
The gaps themselves are enforced by the process-wide `pacing.RateLimiter`, which
the on-demand path shares, so user-driven load queues instead of bursting.

Everything the cycle does is written to `scan_status` as it happens, in plain
language, so the interface can explain a wait instead of showing a spinner.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from . import store
from .config import Config, Source
from .notifier import Notifier
from .pacing import Blocked, RateLimiter, shared_limiter
from .providers import build_provider
from .providers.base import STATUS_STALE, Campsite, Provider, SearchRequest
from .util import iso, utcnow

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    scanned_sources: int = 0
    found: int = 0
    newly_available: int = 0
    pruned: int = 0
    alerts_sent: int = 0
    provider_errors: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    #: Units actually checked, and units abandoned because their host blocked
    #: us. Skipped is not zero-by-default noise — it is the honest count of
    #: what the map does *not* know this cycle.
    scanned_units: int = 0
    skipped_units: int = 0
    blocked: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [
            f"sources={self.scanned_sources}",
            f"found={self.found}",
            f"new={self.newly_available}",
            f"pruned={self.pruned}",
            f"alerts={self.alerts_sent}",
        ]
        if self.skipped_units:
            parts.append(f"skipped={self.skipped_units}")
        if self.provider_errors:
            parts.append(f"errors={len(self.provider_errors)}")
        return " ".join(parts)


@dataclass
class ScanUnit:
    """One campground's worth of work — the granularity of the round-robin.

    Small units are what make interleaving possible. A source that names no
    campgrounds is one unit covering the whole source, because that is all the
    provider will let us ask for.
    """

    source: Source
    provider: Provider
    request: SearchRequest
    label: str
    scope: list[str] = field(default_factory=list)   # campground ids this unit covers


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------

def plan_source(
    conn: sqlite3.Connection,
    source: Source,
    provider: Provider,
    start: date,
    end: date,
    nights: int = 1,
) -> list[ScanUnit]:
    """Split one source into units — one per named campground where possible."""

    def request(campground_ids: list[str]) -> SearchRequest:
        return SearchRequest(
            provider=provider.name,
            start_date=start,
            end_date=end,
            nights=nights,
            rec_area_ids=source.rec_area_ids,
            campground_ids=campground_ids,
        )

    campground_ids = list(source.campground_ids)
    if not campground_ids and getattr(provider, "requires_scope", False):
        # The provider won't crawl blind, so take the scope from the catalog —
        # the known universe (§8k). Config naming individual parks would be a
        # shortlist, which is exactly how Reehers went missing in the first
        # place; the catalog is the one list that is meant to be complete.
        campground_ids = [
            cg.id for cg in store.list_campgrounds(
                conn, provider=provider.name,
                states=[source.state] if source.state else None,
            )
        ]
        if not campground_ids:
            log.warning(
                "%s needs a scope and the catalog has no %s campgrounds yet — "
                "run catalog-refresh first", source.label, provider.name,
            )
            return []

    if not campground_ids:
        # Nothing to split on. One unit for the source as a whole.
        return [
            ScanUnit(source=source, provider=provider, request=request([]),
                     label=source.label, scope=[])
        ]

    units = []
    for cg_id in campground_ids:
        catalogued = store.get_campground(conn, provider.name, cg_id)
        units.append(
            ScanUnit(
                source=source,
                provider=provider,
                request=request([cg_id]),
                # Name it from the catalog so the progress line reads
                # "Reehers Camp Horse Camp", not "412704".
                label=catalogued.name if catalogued else cg_id,
                scope=[cg_id],
            )
        )
    return units


def plan_scan(
    conn: sqlite3.Connection,
    config: Config,
    start: date,
    end: date,
    nights: int = 1,
    provider_factory=build_provider,
    report: Optional[ScanReport] = None,
) -> list[list[ScanUnit]]:
    """One queue per source. The scanner takes one unit from each in turn."""
    report = report or ScanReport()
    queues: list[list[ScanUnit]] = []
    for source in config.sources:
        try:
            provider = provider_factory(source.provider, state=source.state)
        except Exception as exc:  # noqa: BLE001
            # A misconfigured source must not take the whole cycle down with
            # it — the other sources still have honest work to do.
            log.warning("cannot build provider for %s: %s", source.label, exc)
            report.provider_errors[source.label] = str(exc)
            continue
        units = plan_source(conn, source, provider, start, end, nights=nights)
        if units:
            queues.append(units)
            report.scanned_sources += 1
    return queues


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------

class _Progress:
    """Writes `scan_status` as the cycle runs (docs/scanning-design.md).

    Deliberately plain-spoken: "Checking 8 campgrounds — 3 done" beats a
    spinner, and a stated reason beats an unexplained pause.
    """

    def __init__(self, conn, total: int, now: Optional[datetime] = None):
        self.conn = conn
        self.total = total
        self.done = 0
        self.now = now
        self.started = iso(now)
        self.provider: Optional[str] = None
        self.target: Optional[str] = None

    def _write(self, state: str, message: str, detail: Optional[str] = None) -> None:
        store.set_scan_status(
            self.conn,
            store.ScanStatus(
                state=state,
                provider=self.provider,
                target=self.target,
                done=self.done,
                total=self.total,
                message=message,
                detail=detail,
                started=self.started,
            ),
            now=self.now,
        )

    def _counts(self) -> str:
        noun = "campground" if self.total == 1 else "campgrounds"
        return f"Checking {self.total} {noun} — {self.done} done"

    def starting(self, unit: ScanUnit) -> None:
        self.provider = unit.provider.name
        self.target = unit.label
        self._write(store.SCAN_SCANNING, self._counts(), f"Checking {unit.label}")

    def waiting(self, host: str, seconds: float, label: Optional[str]) -> None:
        """Fired by the rate limiter before it sleeps — the honest reason."""
        self._write(
            store.SCAN_WAITING,
            self._counts(),
            f"Waiting {seconds:.0f}s before the next request to {host}",
        )

    def finished(self, unit: ScanUnit) -> None:
        self.done += 1
        self._write(store.SCAN_SCANNING, self._counts(), f"Checked {unit.label}")

    def blocked(self, unit: ScanUnit, reason: str, remaining: int) -> None:
        self.provider = unit.provider.name
        left = f"{remaining} left unchecked" if remaining else "nothing left to check"
        self._write(
            store.SCAN_BLOCKED,
            self._counts(),
            f"{unit.provider.name} asked us to stop, so we did — {left}. {reason}",
        )

    def idle(self, report: ScanReport) -> None:
        self.provider = None
        self.target = None
        noun = "campground" if self.done == 1 else "campgrounds"
        message = f"Checked {self.done} {noun}"
        detail = None
        if report.skipped_units:
            detail = (
                f"{report.skipped_units} skipped because a website asked us to "
                f"slow down — they will show as last checked earlier"
            )
        self._write(store.SCAN_IDLE, message, detail)


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------

def run_unit(
    conn: sqlite3.Connection,
    unit: ScanUnit,
    report: Optional[ScanReport] = None,
    now: Optional[datetime] = None,
) -> list[Campsite]:
    """Search one unit and persist what it returns. Returns the NEW sites.

    Fetches at the broadest level the source supports and filters locally
    (§8k) — provider-side facets are not trusted to be complete.
    """
    report = report or ScanReport()
    sites = unit.provider.search(unit.request)
    for site in sites:
        if site.state is None:
            site.state = unit.source.state
    new = store.upsert_availability(conn, sites, now=now)
    report.found += len(sites)
    report.newly_available += len(new)
    report.scanned_units += 1
    _stamp_catalog_statuses(
        conn, unit.provider.name, unit.source, unit.scope, report,
        seen_ids={s.facility_id for s in sites if s.facility_id}, now=now,
    )
    return new


def run_plan(
    conn: sqlite3.Connection,
    queues: list[list[ScanUnit]],
    report: Optional[ScanReport] = None,
    round_pause: float = 0.0,
    limiter: Optional[RateLimiter] = None,
    now: Optional[datetime] = None,
) -> list[Campsite]:
    """Work the queues round-robin: one unit per source, then round again.

    Finishing one provider before starting the next would stack every request
    to a single host back to back. Interleaving spreads them out for free.
    """
    report = report or ScanReport()
    limiter = limiter or shared_limiter()
    queues = [list(q) for q in queues]
    total = sum(len(q) for q in queues)
    progress = _Progress(conn, total=total, now=now)
    fresh: list[Campsite] = []

    previous_on_wait = limiter.on_wait
    limiter.on_wait = progress.waiting
    try:
        while any(queues):
            worked = False
            for queue in queues:
                if not queue:
                    continue
                unit = queue.pop(0)
                worked = True
                progress.starting(unit)
                try:
                    fresh.extend(run_unit(conn, unit, report, now=now))
                except Blocked as exc:
                    # Stop dead. Everything still queued for this source is
                    # abandoned for the cycle and marked stale, because "we
                    # didn't look" must never read as "there's nothing there".
                    reason = str(exc)
                    log.warning("blocked on %s: %s", unit.provider.name, reason)
                    report.blocked[unit.source.label] = reason
                    report.provider_errors[unit.source.label] = reason
                    abandoned = [unit] + queue
                    report.skipped_units += len(abandoned)
                    for pending in abandoned:
                        _mark_stale(conn, pending, "blocked by provider", now=now)
                    progress.blocked(unit, reason, remaining=len(queue))
                    queue.clear()
                    continue
                except Exception as exc:  # noqa: BLE001 - a dead source degrades, never empties
                    log.warning("scan failed for %s: %s", unit.label, exc)
                    report.provider_errors[unit.source.label] = str(exc)
                    _mark_stale(conn, unit, "live check failed", now=now)
                progress.finished(unit)
            # A pause between rounds, on top of the per-host spacing. Cheap
            # politeness on a run nobody is waiting for.
            if worked and any(queues):
                limiter.pause(round_pause)
    finally:
        limiter.on_wait = previous_on_wait
        # Written even if the cycle is interrupted: "Checked 3 campgrounds" is
        # still true, and a status left reading "scanning" forever is not.
        progress.idle(report)
    return fresh


def scan_source(
    conn: sqlite3.Connection,
    source: Source,
    start: date,
    end: date,
    nights: int = 1,
    provider_factory=build_provider,
    report: Optional[ScanReport] = None,
    now: Optional[datetime] = None,
) -> list[Campsite]:
    """Scan a single source end to end. Paced, but with nothing to interleave.

    Kept for callers that want one source on its own; `scan_once` goes through
    `plan_scan`/`run_plan` so it can round-robin.
    """
    report = report or ScanReport()
    try:
        provider = provider_factory(source.provider, state=source.state)
    except Exception as exc:  # noqa: BLE001
        report.provider_errors[source.label] = str(exc)
        return []
    units = plan_source(conn, source, provider, start, end, nights=nights)
    return run_plan(conn, [units], report=report, now=now)


def _mark_stale(
    conn: sqlite3.Connection,
    unit: ScanUnit,
    reason: str,
    now: Optional[datetime] = None,
) -> None:
    """A failed or skipped check downgrades pins to stale — it never deletes.

    Scoped exactly like the success path, and for the same reason. A unit that
    fails can only speak for what it was going to check:

      * named campgrounds     -> mark those;
      * rec-area-scoped       -> mark NOTHING. We cannot tell which campgrounds
        the source covers, so we cannot attribute the failure to any of them;
      * whole provider+state  -> mark those.

    Found by the first end-to-end scan: one source failing ("Gifford Pinchot
    NF: No campgrounds found to search") marked all 545 recreation.gov
    campgrounds stale, including Clackamas Lake — which had just returned 556
    open site-nights. The map would have said "we couldn't check this" about a
    campground we had successfully checked seconds earlier.
    """
    provider_name = unit.provider.name
    if unit.scope:
        targets = unit.scope
    elif unit.source.rec_area_ids:
        log.debug(
            "%s: %s failed, but its coverage is unknown — not marking anything "
            "stale", provider_name, unit.source.label,
        )
        return
    else:
        targets = [
            cg.id for cg in store.list_campgrounds(
                conn, provider=provider_name,
                states=[unit.source.state] if unit.source.state else None,
            )
        ]

    for cg_id in targets:
        # Never downgrade something this very cycle read successfully. Belt and
        # braces: even if the scoping above is wrong one day, a campground with
        # fresh availability must not be reported as unchecked.
        if _has_fresh_availability(conn, provider_name, cg_id, now):
            continue
        store.set_campground_status(
            conn, provider_name, cg_id, STATUS_STALE, reason, now=now
        )


def _has_fresh_availability(
    conn: sqlite3.Connection,
    provider: str,
    cg_id: str,
    now: Optional[datetime],
) -> bool:
    """Did this cycle just read openings for this campground?"""
    if now is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM availability WHERE provider=? AND facility_id=? "
        "AND last_seen=? LIMIT 1",
        (provider, cg_id, iso(now)),
    ).fetchone()
    return row is not None


def _stamp_catalog_statuses(
    conn: sqlite3.Connection,
    provider_name: str,
    source: Source,
    scope: Iterable[str],
    report: ScanReport,
    seen_ids: Optional[set[str]] = None,
    now: Optional[datetime] = None,
) -> None:
    """Stamp only the campgrounds this unit can honestly speak for.

    "No availability rows" means "we looked and found nothing" — but only for
    campgrounds we actually queried. A source scoped to one rec area used to
    stamp **every** campground the provider has in that state, so scanning Mt
    Hood marked coastal campgrounds hundreds of miles away as `full`. The map
    was asserting knowledge it did not have, which is the same failure as
    calling a first-come site full, at a larger scale.

    Three cases:
      * the unit named campgrounds  -> stamp exactly those;
      * the source covers the whole provider+state -> stamp all of them;
      * the source is rec-area-scoped and we cannot tell which campgrounds that
        covers -> stamp only the ones that came back, and leave the rest alone.

    The third case is a known limitation, not a design: `Campground` does not
    record which rec area it belongs to, so the covered set is genuinely
    unknown. Until it does, silence beats a confident wrong answer.
    """
    scoped = set(scope) or set(source.campground_ids)
    indeterminate = not scoped and bool(source.rec_area_ids)
    if indeterminate:
        scoped = set(seen_ids or ())
        if not scoped:
            log.debug(
                "%s: rec-area-scoped source %s returned nothing; not stamping "
                "campgrounds it may never have queried",
                provider_name, source.label,
            )
            return

    catalogued = store.list_campgrounds(
        conn, provider=provider_name, states=[source.state] if source.state else None
    )
    for cg in catalogued:
        if scoped and cg.id not in scoped:
            continue
        status = store.stamp_status_from_availability(
            conn, cg.provider, cg.id, checked_ok=True, now=now
        )
        report.statuses[f"{cg.provider}|{cg.id}"] = status


def run_watches(
    conn: sqlite3.Connection,
    fresh: Iterable[Campsite],
    notifier: Notifier,
    config: Optional[Config] = None,
    report: Optional[ScanReport] = None,
    now: Optional[datetime] = None,
) -> int:
    """Evaluate active watches and dispatch alerts (§8b).

    Autonomous watches run over the availability the scan already fetched — no
    extra upstream calls. Targeted watches match on the same set here; a
    focused re-search is only needed for scopes outside the scanned window.
    """
    report = report or ScanReport()
    home = config.home_point if config else None
    default_targets = config.default_notify_targets if config else []
    fresh = list(fresh)
    sent = 0

    for watch in store.list_watches(conn, active_only=True):
        matches = store.watch_matches(watch, fresh, home_base=home)
        pending = store.pending_notifications(conn, watch, matches, now=now)
        if not pending:
            continue
        targets = watch.notify_targets or default_targets
        # One digest per watch per cycle whenever a cycle turns up more than a
        # single opening. §8b specifies batching only for autonomous watches,
        # but its stated reason — "so a popular weekend opening doesn't spam
        # you" — applies just as much to a targeted watch, which in practice
        # can match a dozen site-nights at once. Every booking link still
        # travels in the digest, so speed to book is unaffected.
        batch = len(pending) > 1
        sent += notifier.send_sites(
            pending, targets=targets, batch=batch, title=watch.name or "CampgroundFinder"
        )
        for site in pending:
            store.record_notification(conn, watch.id, site.key, now=now)

    report.alerts_sent += sent
    return sent


def scan_once(
    conn: sqlite3.Connection,
    config: Config,
    notifier: Optional[Notifier] = None,
    start: Optional[date] = None,
    window_days: Optional[int] = None,
    nights: int = 1,
    provider_factory=build_provider,
    limiter: Optional[RateLimiter] = None,
    now: Optional[datetime] = None,
) -> ScanReport:
    """One full cycle: plan → round-robin scan → stamp catalog → alert → prune."""
    now = now or utcnow()
    start = start or now.date()
    # `is None`, not `or` — a 0-day window (just tonight) is a real request.
    if window_days is None:
        window_days = config.default_window_days
    end = start + timedelta(days=window_days)
    notifier = notifier or Notifier(config.default_notify_targets)

    # Config-declared watches are seeds (§8c) — inserted once if absent.
    store.seed_watches(conn, config.watches, now=now)

    report = ScanReport()
    queues = plan_scan(
        conn, config, start, end, nights=nights,
        provider_factory=provider_factory, report=report,
    )
    fresh = run_plan(
        conn, queues, report=report,
        round_pause=config.round_pause_seconds, limiter=limiter, now=now,
    )

    run_watches(conn, fresh, notifier, config=config, report=report, now=now)

    # Prune rows not re-confirmed this cycle. The campground stays catalogued
    # and drops to full/unknown — it never disappears (§8 cycle step 3).
    # Everything re-confirmed this cycle carries last_seen == now exactly, so
    # a strict `<` prunes precisely the rows this cycle did not see.
    report.pruned = store.prune_availability(conn, older_than=now)
    return report
